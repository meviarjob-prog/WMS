"""Клиент WB API для поставок FBW (supplies-api.wildberries.ru) — только
чтение: список поставок со статусами, итоги приёмки по товарам и
расхождения по коробам. Методы и поля сверены по официальной спецификации
WB (orders-fbw, снимок от 2026-10-18), а не угаданы.

Авторизация — токен в заголовке Authorization (без «Bearer»). Нужен
токен с категорией «Поставки»; метод расхождений по коробам доступен
только персональному или сервисному токену.

Лимиты (на аккаунт продавца, персональный/сервисный токен):
  - список поставок, детали, товары, упаковка — 30 запросов в минуту,
    интервал 2 с, всплеск 10;
  - расхождения по коробам — 1 запрос в минуту.
Ответ 4xx WB засчитывает как 10 запросов — поэтому 4xx не повторяем.
При 429 ждём ровно X-Ratelimit-Retry секунд (не меньше).
"""

import threading
import time

import requests

WB_SUPPLIES_API_BASE = "https://supplies-api.wildberries.ru"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
MAX_RETRY_WAIT = 65

# Статус поставки (statusID).
WB_SUPPLY_STATUSES = {
    1: "Не запланировано",
    2: "Запланировано",
    3: "Отгрузка разрешена",
    4: "Идёт приёмка",
    5: "Принято",
    6: "Отгружено на воротах",
}
WB_SUPPLY_ACCEPTED = 5


class WbApiError(Exception):
    pass


class WbNotFound(WbApiError):
    pass


class _Limiter:
    """Ведро токенов: capacity запросов подряд, дальше — не чаще interval.
    Общее на процесс, чтобы фоновая проверка и кнопка в интерфейсе не
    превысили лимит аккаунта вместе."""

    def __init__(self, capacity, interval):
        self.capacity = capacity
        self.interval = interval
        self.tokens = float(capacity)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def wait(self, _sleep=time.sleep, deadline=None):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) / self.interval)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                need = (1 - self.tokens) * self.interval
            if deadline is not None and time.monotonic() + need > deadline:
                raise WbApiError("WB API: лимит запросов — не успеваем в отведенное время")
            _sleep(need)


# Запас под лимиты WB: 9 подряд и не чаще раза в 2,1 с; расхождения — раз в 61 с.
_general_limiter = _Limiter(capacity=9, interval=2.1)
_discrepancy_limiter = _Limiter(capacity=1, interval=61)


def wb_request(token, method, path, params=None, payload=None, timeout=None, max_retries=None,
               deadline=None, limiter=None, _sleep=time.sleep):
    if not token:
        raise WbApiError("WB API не настроен — укажите токен на странице «Обмен с WB»")
    timeout = REQUEST_TIMEOUT if timeout is None else timeout
    max_retries = MAX_RETRIES if max_retries is None else max_retries
    limiter = limiter or _general_limiter
    url = f"{WB_SUPPLIES_API_BASE}{path}"
    headers = {"Authorization": token, "Accept": "application/json"}
    last_error = None
    for attempt in range(max_retries):
        left = None if deadline is None else deadline - time.monotonic()
        if left is not None and left <= 0:
            raise WbApiError(f"{path}: WB API не ответил за отведенное время"
                             + (f" ({last_error})" if last_error else ""))
        limiter.wait(_sleep=_sleep, deadline=deadline)
        attempt_timeout = timeout if left is None else max(min(timeout, left), 1)
        try:
            resp = requests.request(method, url, headers=headers, params=params, json=payload,
                                    timeout=attempt_timeout)
        except requests.RequestException as exc:
            last_error = f"не удалось связаться с WB API: {exc}"
            _sleep(min(2 ** attempt, MAX_RETRY_WAIT))
            continue
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError as exc:
                raise WbApiError(f"{path}: WB API вернул не-JSON ответ") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            retry = resp.headers.get("X-Ratelimit-Retry") or resp.headers.get("Retry-After")
            try:
                wait = float(retry)
            except (TypeError, ValueError):
                wait = 2 ** attempt
            last_error = f"{path}: {resp.status_code}"
            if wait > MAX_RETRY_WAIT:
                raise WbApiError(f"{path}: WB просит подождать {wait:g} с (лимит запросов)")
            if deadline is not None and time.monotonic() + wait > deadline:
                raise WbApiError(f"{path}: WB API — лимит запросов, не успеваем в отведенное время")
            _sleep(wait)
            continue
        if resp.status_code == 404:
            raise WbNotFound(f"{path}: не найдено (404)")
        if resp.status_code == 401:
            raise WbApiError("WB API: токен не принят (401) — проверьте токен на странице «Обмен с WB»")
        # Прочие 4xx не повторяем: WB считает каждый как 10 запросов.
        raise WbApiError(f"{path}: ошибка {resp.status_code}: {resp.text[:300]}")
    raise WbApiError(f"{path}: не удалось после {max_retries} попыток ({last_error})")


def fetch_wb_supplies(token, status_ids=None, **kw):
    """Последние поставки (до 1000) — [{supplyID, preorderID, statusID, ...}]."""
    body = {}
    if status_ids:
        body["statusIDs"] = list(status_ids)
    data = wb_request(token, "POST", "/api/v1/supplies", params={"limit": 1000}, payload=body, **kw)
    return data if isinstance(data, list) else []


def fetch_wb_supply_details(token, supply_id, preorder=False, **kw):
    return wb_request(
        token, "GET", f"/api/v1/supplies/{int(supply_id)}",
        params={"isPreorderID": "true" if preorder else "false"}, **kw,
    ) or {}


def fetch_wb_supply_goods(token, supply_id, preorder=False, **kw):
    """Товары поставки: [{barcode, vendorCode, quantity, acceptedQuantity, ...}]."""
    items = []
    offset = 0
    for _ in range(50):
        page = wb_request(
            token, "GET", f"/api/v1/supplies/{int(supply_id)}/goods",
            params={"limit": 1000, "offset": offset, "isPreorderID": "true" if preorder else "false"}, **kw,
        )
        page = page if isinstance(page, list) else []
        items.extend(page)
        if len(page) < 1000:
            break
        offset += 1000
    return items


def fetch_wb_supply_packages(token, supply_id, **kw):
    """Короба поставки: [{packageCode, quantity, barcodes: [{barcode, quantity}]}]."""
    data = wb_request(token, "GET", f"/api/v1/supplies/{int(supply_id)}/package", **kw)
    return data if isinstance(data, list) else []


def fetch_wb_supply_discrepancies(token, supply_id, **kw):
    """Расхождения приёмки по коробам: [{packageCode, items: [{declaredSku,
    declaredAmount, actualSku, actualAmount, discrepancyType}]}]. Лимит —
    1 запрос в минуту, поэтому свой ограничитель."""
    kw.setdefault("limiter", _discrepancy_limiter)
    data = wb_request(token, "GET", f"/api/supplies/v1/discrepancies/{int(supply_id)}", **kw)
    return data if isinstance(data, list) else []
