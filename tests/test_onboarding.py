"""Страница «Первый день на складе» — своя страница сайта (не внешняя
ссылка), см. wms/blueprints/onboarding.py. Должна быть доступна любому
вошедшему сотруднику независимо от роли или ограничений по разделам."""

from wms.extensions import db
from wms.models import User


def test_guide_requires_login(client):
    resp = client.get("/onboarding/", follow_redirects=True)
    assert "Логин" in resp.get_data(as_text=True) or resp.request.path == "/login"


def test_guide_reachable_by_ordinary_staff(db, client_logged_in):
    resp = client_logged_in.get("/onboarding/")
    assert resp.status_code == 200
    assert "Первый день на складе" in resp.get_data(as_text=True)


def test_guide_reachable_by_section_restricted_user(db, client):
    user = User(username="restricted-staff", full_name="Ограниченный", role="warehouse", allowed_sections="none")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True

    resp = client.get("/onboarding/")
    assert resp.status_code == 200


def test_guide_reachable_by_production_only_user(db, client):
    """Роль "производство" обычно заблокирована везде, кроме /production/
    (см. before_request в wms/__init__.py) — обучение из этого исключено."""
    user = User(username="prod-staff", full_name="Производство", role="production")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True

    resp = client.get("/onboarding/")
    assert resp.status_code == 200

    # Контрольная проверка: остальные разделы для этой роли по-прежнему
    # закрыты — исключение сделано именно и только для обучения.
    blocked = client.get("/movement/", follow_redirects=True)
    assert blocked.request.path == "/production/"
