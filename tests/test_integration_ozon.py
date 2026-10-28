"""Настройки интеграции с Ozon Seller API (см. чат: "нужно реализовать
загрузку приемки из озон через апи") — хранение Client-Id/Api-Key
(wms.blueprints.integration_ozon) и клиент API с авторизацией, повторами
и заявками на поставку (wms.utils.ozon_api), сверенный с присланной
спецификацией метода. Акт приёмки (состав по штрихкодам/количествам) пока
не реализован — см. docstring wms/utils/ozon_api.py."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, call, patch

import pytest

from wms.blueprints.integration_ozon import get_ozon_credentials
from wms.extensions import db
from wms.models import User
from wms.utils.ozon_api import (
    OzonApiError,
    check_key,
    fetch_postings,
    fetch_supply_orders,
    ozon_request,
    to_msk,
)


def _staff_user(username="ozon-staff"):
    user = User(username=username, full_name="Сотрудник", role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


# ---------- Настройки (хранение ключа) ----------


def test_settings_page_requires_admin(db, client):
    user = _staff_user()
    _login(client, user)

    resp = client.get("/integrations/ozon/", follow_redirects=True)
    assert "доступны только администратору" in resp.get_data(as_text=True)

    resp = client.post(
        "/integrations/ozon/",
        data={"client_id": "123", "api_key": "secret"},
        follow_redirects=True,
    )
    assert "доступны только администратору" in resp.get_data(as_text=True)
    assert get_ozon_credentials() == (None, None)


def test_admin_can_save_and_reload_credentials(db, client_logged_in):
    assert get_ozon_credentials() == (None, None)

    resp = client_logged_in.post(
        "/integrations/ozon/",
        data={"client_id": "12345", "api_key": "top-secret-key"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "сохранен" in resp.get_data(as_text=True)
    assert get_ozon_credentials() == ("12345", "top-secret-key")

    html = client_logged_in.get("/integrations/ozon/").get_data(as_text=True)
    assert 'value="12345"' in html
    assert 'value="top-secret-key"' in html


def test_check_requires_admin(db, client):
    user = _staff_user("ozon-staff-check")
    _login(client, user)

    resp = client.post("/integrations/ozon/check", follow_redirects=True)
    assert "доступны только администратору" in resp.get_data(as_text=True)


@patch("wms.utils.ozon_api.requests.post")
def test_check_shows_roles_and_expiry(mock_post, db, client_logged_in):
    client_logged_in.post("/integrations/ozon/", data={"client_id": "12345", "api_key": "secret"})
    mock_post.return_value = Mock(
        status_code=200, json=lambda: {"roles": ["ADMIN_READ_ONLY"], "expires_at": "2027-01-27T00:00:00Z"}
    )

    resp = client_logged_in.post("/integrations/ozon/check", follow_redirects=True)

    html = resp.get_data(as_text=True)
    assert "Ключ работает" in html
    assert "ADMIN_READ_ONLY" in html


@patch("wms.utils.ozon_api.requests.post")
def test_check_shows_api_error(mock_post, db, client_logged_in):
    client_logged_in.post("/integrations/ozon/", data={"client_id": "12345", "api_key": "secret"})
    mock_post.return_value = Mock(status_code=401, text="Invalid key")

    resp = client_logged_in.post("/integrations/ozon/check", follow_redirects=True)

    assert "Ozon API:" in resp.get_data(as_text=True)


def test_save_requires_both_fields(db, client_logged_in):
    resp = client_logged_in.post(
        "/integrations/ozon/", data={"client_id": "12345", "api_key": ""}, follow_redirects=True
    )
    assert "Укажите и Client-Id, и Api-Key" in resp.get_data(as_text=True)
    assert get_ozon_credentials() == (None, None)


# ---------- ozon_request: авторизация и повторы ----------


def test_ozon_request_fails_without_credentials():
    with pytest.raises(OzonApiError, match="не настроен"):
        ozon_request(None, None, "/v1/roles")


@patch("wms.utils.ozon_api.requests.post")
def test_ozon_request_sends_auth_headers_and_returns_json(mock_post):
    mock_post.return_value = Mock(status_code=200, json=lambda: {"result": "ok"})

    data = ozon_request("client-1", "key-1", "/v3/supply-order/list", {"a": 1})

    assert data == {"result": "ok"}
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api-seller.ozon.ru/v3/supply-order/list"
    assert kwargs["headers"]["Client-Id"] == "client-1"
    assert kwargs["headers"]["Api-Key"] == "key-1"
    assert kwargs["json"] == {"a": 1}


@patch("wms.utils.ozon_api.requests.post")
def test_ozon_request_retries_429_with_retry_after_then_succeeds(mock_post):
    sleeps = []
    mock_post.side_effect = [
        Mock(status_code=429, headers={"Retry-After": "3"}, text=""),
        Mock(status_code=200, json=lambda: {"ok": True}),
    ]

    data = ozon_request("c", "k", "/v3/supply-order/list", {}, _sleep=sleeps.append)

    assert data == {"ok": True}
    assert sleeps == [3]
    assert mock_post.call_count == 2


@patch("wms.utils.ozon_api.requests.post")
def test_ozon_request_does_not_retry_plain_4xx(mock_post):
    mock_post.return_value = Mock(status_code=400, text="Bad filter")

    with pytest.raises(OzonApiError, match="400"):
        ozon_request("c", "k", "/v3/supply-order/list", {}, _sleep=lambda s: None)

    assert mock_post.call_count == 1


@patch("wms.utils.ozon_api.requests.post")
def test_ozon_request_exhausts_retries_on_repeated_5xx(mock_post):
    mock_post.return_value = Mock(status_code=503, headers={}, text="busy")
    sleeps = []

    with pytest.raises(OzonApiError, match="не удалось после 5 попыток"):
        ozon_request("c", "k", "/v3/supply-order/list", {}, _sleep=sleeps.append)

    assert mock_post.call_count == 5
    assert sleeps == [1, 2, 4, 8, 16]  # 2**попытка


@patch("wms.utils.ozon_api.requests.post")
def test_check_key_calls_roles_endpoint(mock_post):
    mock_post.return_value = Mock(
        status_code=200, json=lambda: {"roles": ["ADMIN_READ_ONLY"], "expires_at": "2027-01-27T00:00:00Z"}
    )

    data = check_key("c", "k")

    assert data["roles"] == ["ADMIN_READ_ONLY"]
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api-seller.ozon.ru/v1/roles"


# ---------- Даты ----------


def test_to_msk_converts_and_blanks_empty_date():
    assert to_msk("2026-10-25T08:00:00Z") == "25.10.2026 11:00"
    assert to_msk("0001-01-01T00:00:00Z") == ""
    assert to_msk(None) == ""
    assert to_msk("") == ""


# ---------- Заявки на поставку: постраничная выборка ----------


def _supply_list_response(order_ids, last_id=""):
    return {"order_ids": order_ids, "last_id": last_id}


def _supply_get_response(orders):
    return {"orders": orders}


def _order(order_id, order_number, state="IN_TRANSIT", updated=None, supply_id="s1"):
    return {
        "order_id": order_id,
        "order_number": order_number,
        "state": state,
        "state_updated_date": updated or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "created_date": updated or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "supplies": [{"supply_id": supply_id, "state": state}],
    }


@patch("wms.utils.ozon_api.requests.post")
def test_supply_orders_pagination_stops_on_empty_order_ids(mock_post):
    mock_post.side_effect = [
        Mock(status_code=200, json=lambda: _supply_list_response([1, 2], last_id="next")),
        Mock(status_code=200, json=lambda: _supply_get_response([_order(1, "A-1"), _order(2, "A-2")])),
        Mock(status_code=200, json=lambda: _supply_list_response([], last_id="")),
    ]

    orders = fetch_supply_orders("c", "k")

    assert [o["order_number"] for o in orders] == ["A-1", "A-2"]
    assert mock_post.call_count == 3


@patch("wms.utils.ozon_api.requests.post")
def test_supply_orders_pagination_stops_on_empty_last_id(mock_post):
    mock_post.side_effect = [
        Mock(status_code=200, json=lambda: _supply_list_response([1], last_id="")),
        Mock(status_code=200, json=lambda: _supply_get_response([_order(1, "A-1")])),
    ]

    orders = fetch_supply_orders("c", "k")

    assert [o["order_number"] for o in orders] == ["A-1"]
    assert mock_post.call_count == 2


@patch("wms.utils.ozon_api.requests.post")
def test_supply_order_get_splits_into_batches_of_50(mock_post):
    ids = list(range(1, 76))  # 75 id -> две пачки: 50 + 25
    orders_batch1 = [_order(i, f"A-{i}") for i in ids[:50]]
    orders_batch2 = [_order(i, f"A-{i}") for i in ids[50:]]
    mock_post.side_effect = [
        Mock(status_code=200, json=lambda: _supply_list_response(ids, last_id="")),
        Mock(status_code=200, json=lambda: _supply_get_response(orders_batch1)),
        Mock(status_code=200, json=lambda: _supply_get_response(orders_batch2)),
    ]

    orders = fetch_supply_orders("c", "k")

    assert len(orders) == 75
    get_calls = [c for c in mock_post.call_args_list if "/v3/supply-order/get" in c.args[0]]
    assert len(get_calls) == 2
    assert len(get_calls[0].kwargs["json"]["order_ids"]) == 50
    assert len(get_calls[1].kwargs["json"]["order_ids"]) == 25


@patch("wms.utils.ozon_api.requests.post")
def test_supply_orders_stop_when_older_than_days_back(mock_post):
    fresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat().replace("+00:00", "Z")
    mock_post.side_effect = [
        Mock(status_code=200, json=lambda: _supply_list_response([1, 2], last_id="next")),
        Mock(
            status_code=200,
            json=lambda: _supply_get_response(
                [_order(1, "A-1", updated=fresh), _order(2, "A-2", updated=old)]
            ),
        ),
        # Если бы выборка не остановилась, пошел бы третий запрос списка —
        # его не должно случиться.
    ]

    orders = fetch_supply_orders("c", "k", days_back=60)

    assert [o["order_number"] for o in orders] == ["A-1"]
    assert mock_post.call_count == 2


@patch("wms.utils.ozon_api.requests.post")
def test_supply_orders_order_number_filter_is_sent(mock_post):
    mock_post.side_effect = [
        Mock(status_code=200, json=lambda: _supply_list_response([1], last_id="")),
        Mock(status_code=200, json=lambda: _supply_get_response([_order(1, "ШМ-005")])),
    ]

    fetch_supply_orders("c", "k", order_number="ШМ-005")

    list_call = mock_post.call_args_list[0]
    assert list_call.kwargs["json"]["filter"]["order_number_search"] == "ШМ-005"


# ---------- Отправления: постраничная выборка по cursor ----------


@patch("wms.utils.ozon_api.requests.post")
def test_supply_orders_days_back_none_disables_age_cutoff(mock_post):
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat().replace("+00:00", "Z")
    mock_post.side_effect = [
        Mock(status_code=200, json=lambda: _supply_list_response([1], last_id="")),
        Mock(status_code=200, json=lambda: _supply_get_response([_order(1, "A-1", updated=old)])),
    ]

    orders = fetch_supply_orders("c", "k", days_back=None, order_number="A-1")

    assert [o["order_number"] for o in orders] == ["A-1"]


@patch("wms.utils.ozon_api.requests.post")
def test_postings_pagination_follows_cursor_while_has_next(mock_post):
    mock_post.side_effect = [
        Mock(status_code=200, json=lambda: {"postings": [{"posting_number": "P-1"}], "has_next": True, "cursor": "c1"}),
        Mock(status_code=200, json=lambda: {"postings": [{"posting_number": "P-2"}], "has_next": False, "cursor": ""}),
    ]

    postings = fetch_postings("c", "k")

    assert [p["posting_number"] for p in postings] == ["P-1", "P-2"]
    assert mock_post.call_count == 2
    second_call = mock_post.call_args_list[1]
    assert second_call.kwargs["json"]["cursor"] == "c1"


@patch("wms.utils.ozon_api.requests.post")
def test_supply_orders_order_number_keeps_only_exact_match(mock_post):
    """Поиск по номеру неточный — похожие номера не должны подмешиваться."""
    mock_post.side_effect = [
        Mock(status_code=200, json=lambda: _supply_list_response([1, 2], last_id="next")),
        Mock(status_code=200, json=lambda: _supply_get_response([_order(1, "200001234"), _order(2, "20000123")])),
    ]
    orders = fetch_supply_orders("c", "k", days_back=None, order_number="20000123")
    assert [o["order_number"] for o in orders] == ["20000123"]
    assert mock_post.call_count == 2      # дальше не листаем


@patch("wms.utils.ozon_api.requests.post")
def test_request_respects_deadline_and_caps_retry_after(mock_post):
    """Ozon может прислать огромный Retry-After — ждем не дольше общего
    лимита операции и не начинаем попытку после него."""
    import time as _time
    from wms.utils.ozon_api import OzonApiError, ozon_request

    mock_post.return_value = Mock(status_code=429, headers={"Retry-After": "600"}, text="")
    waits = []

    def fake_sleep(seconds):
        waits.append(seconds)

    deadline = _time.monotonic() + 0.05
    with pytest.raises(OzonApiError):
        ozon_request("c", "k", "/x", {}, _sleep=lambda s: (fake_sleep(s), _time.sleep(0.03)), timeout=10,
                     max_retries=5, deadline=deadline)
    assert all(w <= 0.05 for w in waits)
    assert mock_post.call_count <= 3
    assert mock_post.call_args.kwargs["timeout"] <= 10


@patch("wms.utils.ozon_api.requests.post")
def test_request_caps_retry_after_without_deadline(mock_post):
    from wms.utils.ozon_api import MAX_RETRY_WAIT, OzonApiError, ozon_request

    mock_post.return_value = Mock(status_code=429, headers={"Retry-After": "600"}, text="")
    waits = []
    with pytest.raises(OzonApiError):
        ozon_request("c", "k", "/x", {}, _sleep=waits.append, timeout=10, max_retries=2)
    assert waits == [MAX_RETRY_WAIT, MAX_RETRY_WAIT]


@patch("wms.utils.ozon_api.requests.post")
def test_supply_orders_stops_when_all_wanted_numbers_found(mock_post):
    mock_post.side_effect = [
        Mock(status_code=200, json=lambda: _supply_list_response([1, 2], last_id="next")),
        Mock(status_code=200, json=lambda: _supply_get_response([_order(1, "A-1"), _order(2, "A-2")])),
    ]
    orders = fetch_supply_orders("c", "k", wanted_numbers={"A-2"})
    assert "A-2" in [o["order_number"] for o in orders]
    assert mock_post.call_count == 2


@patch("wms.utils.ozon_api.requests.post")
def test_cargo_places_statuses_and_bundle_parsing(mock_post):
    from wms.utils.ozon_api import fetch_bundle_items, fetch_cargo_statuses, fetch_supply_cargo_places

    mock_post.side_effect = [
        Mock(status_code=200, json=lambda: {"supplies_cargoes": [{
            "supply_id": 77,
            "cargoes_without_transport_cargoes": [{"cargo_id": 1, "barcode": "GM-1", "bundle_id": "b1"}],
            "transport_cargoes": [{"cargoes": [{"cargo_id": 2, "barcode": "GM-2", "bundle_id": "b2"}]}],
        }]}),
        Mock(status_code=200, json=lambda: {"supplies": [{"supply_id": 77, "cargoes": [
            {"cargo_id": 1, "tracking_info": {"status": "PROCESSED"}},
            {"cargo_id": 2, "tracking_info": {"status": "NOT_DELIVERED"}},
        ]}]}),
        Mock(status_code=200, json=lambda: {"items": [{"barcode": "460", "offer_id": "A", "quantity": 5}],
                                            "has_next": True, "last_id": "x"}),
        Mock(status_code=200, json=lambda: {"items": [{"barcode": "461", "offer_id": "B", "quantity": 2}],
                                            "has_next": False}),
    ]
    places = fetch_supply_cargo_places("c", "k", [77])
    assert [c["barcode"] for c in places[77]] == ["GM-1", "GM-2"]
    statuses = fetch_cargo_statuses("c", "k", {77: [1, 2]})
    assert statuses[2]["status"] == "NOT_DELIVERED"
    assert mock_post.call_args_list[1].kwargs["json"] == {"supplies": [{"supply_id": 77, "cargo_ids": ["1", "2"]}]}
    items = fetch_bundle_items("c", "k", "b1")
    assert [(i["barcode"], i["quantity"]) for i in items] == [("460", 5), ("461", 2)]
