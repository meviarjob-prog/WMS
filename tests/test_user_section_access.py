"""Точечный доступ к разделам для пользователей (User.allowed_sections,
см. auth.update_sections) — админ может ограничить не-админа конкретными
разделами верхнего меню; без настройки доступ остается полным (обратная
совместимость для уже существующих пользователей)."""

from wms.extensions import db
from wms.models import User


def _make_staff_user(allowed_sections=None):
    user = User(username="staffer", full_name="Складской", role="warehouse", allowed_sections=allowed_sections)
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    return user


def _login_as(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_no_restriction_means_full_access(db, client):
    user = _make_staff_user(allowed_sections=None)
    _login_as(client, user)

    assert client.get("/movement/").status_code == 200
    assert client.get("/receiving/").status_code == 200
    assert client.get("/shipment-plan/").status_code == 200


def test_restricted_user_blocked_from_other_sections(db, client):
    user = _make_staff_user(allowed_sections="movement")
    _login_as(client, user)

    resp_ok = client.get("/movement/")
    assert resp_ok.status_code == 200

    resp_blocked = client.get("/receiving/", follow_redirects=True)
    html = resp_blocked.get_data(as_text=True)
    assert "не доступен" in html


def test_none_restriction_blocks_all_configurable_sections(db, client):
    user = _make_staff_user(allowed_sections="none")
    _login_as(client, user)

    for path in ("/movement/", "/receiving/", "/placement/", "/inventory/", "/shipment-plan/"):
        resp = client.get(path, follow_redirects=True)
        assert "не доступен" in resp.get_data(as_text=True)


def test_admin_bypasses_section_restriction(db, client, admin_user):
    admin_user.allowed_sections = "none"
    db.session.commit()
    _login_as(client, admin_user)

    assert client.get("/movement/").status_code == 200


def test_nav_hides_restricted_sections(db, client):
    user = _make_staff_user(allowed_sections="movement")
    _login_as(client, user)

    html = client.get("/movement/").get_data(as_text=True)
    assert 'href="/movement/">Перемещение</a>' in html
    assert 'href="/receiving/">Приемка</a>' not in html
    assert 'href="/inventory/">Инвентаризация</a>' not in html


def test_update_sections_route_sets_restriction(db, client_logged_in):
    user = _make_staff_user(allowed_sections=None)

    resp = client_logged_in.post(
        f"/users/{user.id}/sections",
        data={"mode": "restricted", "section_movement": "on", "section_receiving": "on"},
    )
    assert resp.status_code == 302

    user = User.query.get(user.id)
    assert user.allowed_sections in ("movement,receiving", "receiving,movement")
    assert user.has_section_access("movement")
    assert not user.has_section_access("placement")


def test_update_sections_route_full_access_clears_restriction(db, client_logged_in):
    user = _make_staff_user(allowed_sections="movement")

    client_logged_in.post(f"/users/{user.id}/sections", data={"mode": "full"})

    user = User.query.get(user.id)
    assert user.allowed_sections is None
    assert user.has_section_access("placement")
