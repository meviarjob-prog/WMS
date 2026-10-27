"""Клиент Ozon Seller API — аутентификация и общий HTTP-вызов.

Ключ (Client-Id + Api-Key) создается в кабинете продавца: Настройки →
Seller API → генерация ключа. Для задач только на чтение (как загрузка
приемки — см. чат) хватает ключа с ролью "Admin read-only": он не может
ничего изменить на стороне Ozon, даже если утечет.

Конкретные методы (например, получение акта приемки поставки FBO) пока не
реализованы здесь — на момент написания в этом окружении не было доступа
к docs.ozon.ru, чтобы сверить точный путь/формат ответа метода, поэтому
был бы риск угадать и написать по памяти рабочий на вид, но на деле
нерабочий вызов. Здесь только проверенная часть: общая авторизация и
обработка ошибок, одинаковая для любого метода Ozon Seller API.
"""

import requests

OZON_API_BASE = "https://api-seller.ozon.ru"
REQUEST_TIMEOUT = 30


class OzonApiError(Exception):
    """Ошибка на любом этапе вызова Ozon API — сетевая, HTTP-код ответа
    4xx/5xx, или сам Ozon сообщил об ошибке в теле ответа (многие методы
    отвечают 200 даже при ошибке, с полем "message"/"code" внутри — это
    проверяет уже конкретный метод, а не эта общая функция)."""


def ozon_request(client_id, api_key, method, path, payload=None):
    """Запрос к Ozon Seller API с авторизацией по Client-Id/Api-Key.

    method — "GET"/"POST" и т.п.; path — часть адреса после хоста, с
    ведущим "/" (например "/v2/posting/fbo/list"); payload — тело запроса
    (для GET обычно не нужен). Возвращает разобранный JSON-ответ целиком —
    разбор по нужным полям делает конкретный метод-обертка."""
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
    try:
        resp = requests.request(
            method, url, headers=headers, json=payload if payload is not None else {}, timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        raise OzonApiError(f"Не удалось связаться с Ozon API: {exc}") from exc
    if resp.status_code >= 400:
        raise OzonApiError(f"Ozon API вернул ошибку {resp.status_code}: {resp.text[:500]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise OzonApiError("Ozon API вернул не-JSON ответ") from exc
