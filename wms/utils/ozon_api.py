"""Клиент Ozon Seller API — аутентификация, повторы, заявки на поставку
(FBO) и отправления. Правила запросов и структура ответов сверены по
официальной спецификации метода (см. чат — прислано ТЗ trek на основе
рабочего прототипа), не угаданы по памяти.

Базовый адрес: https://api-seller.ozon.ru. Все методы — POST, тело в
JSON. Заголовки: Client-Id, Api-Key, Content-Type: application/json.
Ключ создаётся в кабинете продавца: Настройки → Seller API → генерация
ключа; для задач только на чтение достаточно роли "Admin read-only".
Запросы — только с сервера, из браузера Ozon блокирует их CORS.

Лимит — не больше 50 запросов/сек с одного Client-Id. Правило повторов
(см. _request):
  200          -> вернуть JSON
  429          -> подождать Retry-After (или 2**попытка сек), повторить
  5xx/таймаут  -> подождать 2**попытка сек, повторить
  4xx, не 429  -> не повторять, сразу ошибка (код, метод, начало ответа)
Максимум 5 попыток, таймаут запроса 60 секунд.

Метод получения ФАКТИЧЕСКИ ПРИНЯТОГО количества по каждой позиции
(аналог "акта приёмки", который сейчас загружают файлом — см.
movement.receive_act/utils.acceptance_act) здесь НЕ реализован: в
присланной спецификации есть только отслеживание СТАТУСОВ заявки/
поставки (/v3/supply-order/get возвращает state, даты, таймслот, склад —
но не состав по штрихкодам). Есть упоминание supplies[].bundle_id "для
/v1/supply-order/bundle" — похоже, это и есть состав, но формат запроса/
ответа этого метода в спецификации не описан, поэтому здесь не
реализован — был бы риск угадать и написать нерабочий вызов.
"""

import time
from datetime import datetime, timedelta, timezone

import requests

OZON_API_BASE = "https://api-seller.ozon.ru"
REQUEST_TIMEOUT = 60
MAX_RETRIES = 5
MSK = timezone(timedelta(hours=3))

SUPPLY_STATES = {
    "DATA_FILLING": "Заполнение данных",
    "READY_TO_SUPPLY": "Готова к отгрузке",
    "ACCEPTED_AT_SUPPLY_WAREHOUSE": "Принята на точке отгрузки",
    "IN_TRANSIT": "В пути",
    "ACCEPTANCE_AT_STORAGE_WAREHOUSE": "Приёмка на складе",
    "REPORTS_CONFIRMATION_AWAITING": "Согласование актов",
    "REPORT_REJECTED": "Спор",
    "COMPLETED": "Завершена",
    "REJECTED_AT_SUPPLY_WAREHOUSE": "Отказано в приёмке",
    "CANCELLED": "Отменена",
    "OVERDUE": "Просрочена",
    "UNSPECIFIED": "Не определён",
}
# В фильтр запроса /v3/supply-order/list UNSPECIFIED не передается — он
# встречается только в ответе (см. спецификацию).
_SUPPLY_FILTER_STATES = [s for s in SUPPLY_STATES if s != "UNSPECIFIED"]

POSTING_STATUSES = {
    "awaiting_packaging": "Ожидает упаковки",
    "awaiting_deliver": "Ожидает отгрузки",
    "delivering": "Доставляется",
    "delivered": "Доставлено",
    "cancelled": "Отменено",
}

POSTING_SUBSTATUSES = {
    "posting_split_pending": "Создано",
    "posting_created": "Создано",
    "posting_packing": "На упаковке",
    "posting_transferring_to_delivery": "Передаётся в доставку",
    "posting_on_way_to_city": "В пути в город доставки",
    "posting_returned_to_warehouse": "Возвращено на склад",
    "posting_transferred_to_courier_service": "Передаётся в службу доставки",
    "posting_in_courier_service": "Курьер в пути",
    "posting_on_way_to_pickup_point": "В пути в пункт выдачи",
    "posting_in_pickup_point": "В пункте выдачи",
    "posting_delivered": "Доставлено курьером",
    "posting_received": "Получено в пункте выдачи",
    "posting_canceled": "Отменено",
}


class OzonApiError(Exception):
    """Ошибка вызова Ozon API — отсутствие ключа, исчерпаны попытки
    повтора, или Ozon ответил кодом ошибки без права на повтор (см.
    модуль docstring — таблица повторов)."""


def utc_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_msk(value):
    """Ozon отдаёт время в UTC — переводим в МСК для отображения.
    "0001-01-01T00:00:00Z" и подобные пустые даты -> "" (см. спецификацию:
    такое значение означает "нет данных", а не 1-й год нашей эры)."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.year < 2000:
            return ""
        return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def ozon_request(client_id, api_key, path, payload=None, _sleep=time.sleep):
    """POST-запрос к Ozon Seller API с авторизацией и повторами по
    правилам из спецификации (см. модуль docstring). _sleep — точка
    подмены в тестах, чтобы не ждать реально."""
    if not client_id or not api_key:
        raise OzonApiError(
            "Ozon API не настроен — укажите Client-Id и Api-Key на странице «Обмен с Ozon»"
        )
    url = f"{OZON_API_BASE}{path}"
    headers = {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(
                url, headers=headers, json=payload if payload is not None else {}, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as exc:
            last_error = f"не удалось связаться с Ozon API: {exc}"
            _sleep(2**attempt)
            continue
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError as exc:
                raise OzonApiError(f"{path}: Ozon API вернул не-JSON ответ") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = resp.headers.get("Retry-After")
            wait = int(wait) if wait and wait.isdigit() else 2**attempt
            last_error = f"{path}: {resp.status_code}, ждали {wait} с"
            _sleep(wait)
            continue
        # 4xx, кроме 429 — не повторяем, сразу ошибка.
        raise OzonApiError(f"{path}: ошибка {resp.status_code}: {resp.text[:500]}")
    raise OzonApiError(f"{path}: не удалось после {MAX_RETRIES} попыток ({last_error})")


def check_key(client_id, api_key):
    """Права ключа и срок действия (см. спецификацию: /v1/roles,
    expires_at). Используется и для простой проверки связи ("ключ
    работает"), и чтобы предупредить об истечении заранее (ключ Ozon живет
    3 месяца)."""
    return ozon_request(client_id, api_key, "/v1/roles", {})


def fetch_supply_orders(client_id, api_key, days_back=60, order_number=None):
    """Заявки на поставку (FBO) — постраничная выборка /v3/supply-order/list
    + /v3/supply-order/get, как описано в спецификации:
      - сортировка по дате изменения статуса, по убыванию;
      - как только встретилась заявка старше days_back дней — дальше не
        запрашиваем (остальные еще старее, список отсортирован);
      - order_number — точечный поиск одной заявки (order_number_search),
        например по MovementDocument.marketplace_request_number, вместо
        выгрузки всех заявок за период; days_back=None вместе с ним
        отключает отсечку по возрасту совсем — точечный поиск не должен
        молча терять старую, но все еще нужную заявку.
    Возвращает список "orders" как есть из ответа /v3/supply-order/get
    (см. спецификацию — поля order_id/order_number/state/supplies[]/...).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back) if days_back is not None else None
    result = []
    last_id = ""
    while True:
        body = {
            "filter": {"states": _SUPPLY_FILTER_STATES},
            "limit": 100,
            "sort_by": "ORDER_STATE_UPDATED_AT",
            "sort_dir": "DESC",
        }
        if order_number:
            body["filter"]["order_number_search"] = order_number
        if last_id:
            body["last_id"] = last_id
        data = ozon_request(client_id, api_key, "/v3/supply-order/list", body)
        ids = data.get("order_ids") or []
        if not ids:
            break

        reached_old = False
        for i in range(0, len(ids), 50):  # get принимает до 50 id за раз
            details = ozon_request(client_id, api_key, "/v3/supply-order/get", {"order_ids": ids[i : i + 50]})
            for order in details.get("orders", []):
                if cutoff is not None:
                    updated = order.get("state_updated_date") or order.get("created_date") or ""
                    try:
                        if updated and datetime.fromisoformat(updated.replace("Z", "+00:00")) < cutoff:
                            reached_old = True
                            continue
                    except ValueError:
                        pass
                result.append(order)

        last_id = data.get("last_id") or ""
        if reached_old or not last_id:
            break
    return result


def fetch_postings(client_id, api_key, days_back=30):
    """Отправления покупателям со складов Ozon — постраничная выборка
    /v3/posting/fbo/list по cursor, пока has_next=true. Период не больше
    года (иначе Ozon отвечает ошибкой PERIOD_IS_TOO_LONG)."""
    now = datetime.now(timezone.utc)
    body = {
        "filter": {"since": utc_iso(now - timedelta(days=days_back)), "to": utc_iso(now)},
        "limit": 100,
        "sort_dir": "DESC",
        "with": {"analytics_data": True},
    }
    result = []
    while True:
        data = ozon_request(client_id, api_key, "/v3/posting/fbo/list", body)
        result.extend(data.get("postings") or [])
        cursor = data.get("cursor")
        if not data.get("has_next") or not cursor:
            break
        body["cursor"] = cursor
    return result


# Тип акта (см. ответ /v1/supply-order/act/product/get, items[].sku_info —
# товар, ACCEPTANCE — акт приемки годного, остальные относятся к браку/
# расхождениям; состав поставки по грузоместам (коробам) этот метод не
# возвращает вообще — только суммарно по товару за всю поставку, без
# привязки к тому, в каком именно коробе товар физически лежал.
SUPPLY_ACT_TYPES = {
    "UNSPECIFIED": "Не определён",
    "ACCEPTANCE": "Акт приёмки",
    "DEFECT": "Акт о браке",
    "SURPLUS": "Акт об излишках",
    "SHORTCOMING": "Акт о недостаче",
}


def fetch_supply_act_items(client_id, api_key, supply_id):
    """Строки актов поставки (/v1/supply-order/act/product/get) в плоском
    виде — по каждому товару declared/approved/fact_quantity и тип акта,
    вне зависимости от короба (API не возвращает эту привязку). Возвращает
    (items, unidentified_quantity) — вторая величина ненулевая, если в
    поставке есть неопознанные излишки (Ozon принял что-то, что не смог
    сопоставить ни с одной позицией нашего заказа) — тогда сумма по
    товарам неполная, сверка по ней ненадёжна."""
    data = ozon_request(client_id, api_key, "/v1/supply-order/act/product/get", {"supply_id": int(supply_id)})
    items = []
    unidentified = 0
    for act in data.get("supply_acts") or []:
        unidentified += act.get("unidentified_quantity") or 0
        act_type = act.get("type") or "UNSPECIFIED"
        for it in act.get("items") or []:
            sku_info = it.get("sku_info") or {}
            items.append(
                {
                    "type": act_type,
                    "barcode": sku_info.get("barcode") or "",
                    "offer_id": sku_info.get("offer_id") or "",
                    "sku": sku_info.get("sku"),
                    "name": sku_info.get("name") or "",
                    "declared_quantity": it.get("declared_quantity") or 0,
                    "approved_quantity": it.get("approved_quantity") or 0,
                    "fact_quantity": it.get("fact_quantity") or 0,
                }
            )
    return items, unidentified
