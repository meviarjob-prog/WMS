"""Настройки интеграции с Ozon Seller API (см. чат: "нужно реализовать
загрузку приемки из озон через апи") — пока только хранение Client-Id/
Api-Key (wms.blueprints.integration_ozon) и общий HTTP-вызов с
авторизацией (wms.utils.ozon_api). Сам метод получения акта приемки еще
не реализован — см. docstring wms/utils/ozon_api.py."""

from unittest.mock import Mock, patch

import pytest

from wms.blueprints.integration_ozon import get_ozon_credentials
from wms.extensions import db
from wms.models import User
from wms.utils.ozon_api import OzonApiError, ozon_request


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

    # Форма при перезагрузке показывает уже сохраненные значения.
    html = client_logged_in.get("/integrations/ozon/").get_data(as_text=True)
    assert 'value="12345"' in html
    assert 'value="top-secret-key"' in html


def test_save_requires_both_fields(db, client_logged_in):
    resp = client_logged_in.post(
        "/integrations/ozon/", data={"client_id": "12345", "api_key": ""}, follow_redirects=True
    )
    assert "Укажите и Client-Id, и Api-Key" in resp.get_data(as_text=True)
    assert get_ozon_credentials() == (None, None)


def test_ozon_request_fails_without_credentials():
    with pytest.raises(OzonApiError, match="не настроен"):
        ozon_request(None, None, "GET", "/v2/posting/fbo/list")


@patch("wms.utils.ozon_api.requests.request")
def test_ozon_request_sends_auth_headers_and_returns_json(mock_request):
    mock_request.return_value = Mock(status_code=200, json=lambda: {"result": "ok"})

    data = ozon_request("client-1", "key-1", "POST", "/v2/posting/fbo/list", {"a": 1})

    assert data == {"result": "ok"}
    args, kwargs = mock_request.call_args
    assert args[0] == "POST"
    assert args[1] == "https://api-seller.ozon.ru/v2/posting/fbo/list"
    assert kwargs["headers"]["Client-Id"] == "client-1"
    assert kwargs["headers"]["Api-Key"] == "key-1"
    assert kwargs["json"] == {"a": 1}


@patch("wms.utils.ozon_api.requests.request")
def test_ozon_request_raises_on_http_error(mock_request):
    mock_request.return_value = Mock(status_code=403, text="Forbidden")

    with pytest.raises(OzonApiError, match="403"):
        ozon_request("client-1", "key-1", "GET", "/v2/posting/fbo/list")


@patch("wms.utils.ozon_api.requests.request")
def test_ozon_request_raises_on_non_json_response(mock_request):
    mock_request.return_value = Mock(status_code=200, json=Mock(side_effect=ValueError("bad json")))

    with pytest.raises(OzonApiError, match="не-JSON"):
        ozon_request("client-1", "key-1", "GET", "/v2/posting/fbo/list")
