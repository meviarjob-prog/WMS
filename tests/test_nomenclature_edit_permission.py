"""Право редактировать номенклатуру (User.nomenclature_edit_allowed,
см. nomenclature._require_edit) — отдельно от доступа к разделу
"nomenclature" как таковому: без этого флага пользователь по-прежнему
видит список и "Где товар", но не может создавать/менять товары или
импортировать их из Excel. Настраивается в «Настройки» → «Разделы»
(auth.update_sections), галочкой независимой от режима доступа к разделам."""

from wms.extensions import db
from wms.models import Nomenclature, User


def _make_staff_user(nomenclature_edit_allowed=True):
    user = User(
        username="editor-staffer",
        full_name="Складской",
        role="warehouse",
        nomenclature_edit_allowed=nomenclature_edit_allowed,
    )
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    return user


def _login_as(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_new_user_can_edit_nomenclature_by_default(db):
    """Обратная совместимость: у всех, кто мог редактировать номенклатуру
    до появления этого флага, право должно остаться — значит, по
    умолчанию (для новых пользователей тоже) флаг включен."""
    user = User(username="brandnew", role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()

    assert user.nomenclature_edit_allowed is True
    assert user.can_edit_nomenclature() is True


def test_admin_can_edit_regardless_of_flag(db, admin_user):
    admin_user.nomenclature_edit_allowed = False
    db.session.commit()

    assert admin_user.can_edit_nomenclature() is True


def test_create_nomenclature_blocked_without_permission(db, client):
    user = _make_staff_user(nomenclature_edit_allowed=False)
    _login_as(client, user)

    resp = client.post(
        "/nomenclature/create",
        data={"barcode": "1112223334445", "name": "Тестовый товар"},
        follow_redirects=True,
    )

    assert "не разрешено" in resp.get_data(as_text=True)
    assert Nomenclature.query.filter_by(barcode="1112223334445").first() is None


def test_create_nomenclature_allowed_with_permission(db, client):
    user = _make_staff_user(nomenclature_edit_allowed=True)
    _login_as(client, user)

    client.post(
        "/nomenclature/create",
        data={"barcode": "1112223334446", "name": "Тестовый товар 2"},
    )

    assert Nomenclature.query.filter_by(barcode="1112223334446").first() is not None


def test_update_category_blocked_without_permission(db, client):
    item = Nomenclature(sku="SKU-EDIT-1", barcode="2223334445556", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    user = _make_staff_user(nomenclature_edit_allowed=False)
    _login_as(client, user)

    client.post(f"/nomenclature/{item.id}/category", data={"category_id": ""})

    assert Nomenclature.query.get(item.id).category_id is None


def test_update_norm_blocked_without_permission(db, client):
    item = Nomenclature(sku="SKU-EDIT-2", barcode="3334445556667", name="Товар 2", unit="шт")
    db.session.add(item)
    db.session.commit()

    user = _make_staff_user(nomenclature_edit_allowed=False)
    _login_as(client, user)

    client.post(f"/nomenclature/{item.id}/norm", data={"norm_minutes": "5"})

    assert Nomenclature.query.get(item.id).norm_minutes is None


def test_update_barcode_blocked_without_permission(db, client):
    item = Nomenclature(sku="SKU-EDIT-3", barcode="4445556667778", name="Товар 3", unit="шт")
    db.session.add(item)
    db.session.commit()

    user = _make_staff_user(nomenclature_edit_allowed=False)
    _login_as(client, user)

    client.post(f"/nomenclature/{item.id}/barcode", data={"barcode": "9998887776665"})

    assert Nomenclature.query.get(item.id).barcode == "4445556667778"


def test_update_barcode_allowed_with_permission(db, client):
    item = Nomenclature(sku="SKU-EDIT-4", barcode="5556667778889", name="Товар 4", unit="шт")
    db.session.add(item)
    db.session.commit()

    user = _make_staff_user(nomenclature_edit_allowed=True)
    _login_as(client, user)

    client.post(f"/nomenclature/{item.id}/barcode", data={"barcode": "1112223334445"})

    assert Nomenclature.query.get(item.id).barcode == "1112223334445"


def test_update_barcode_rejects_duplicate(db, client_logged_in):
    item1 = Nomenclature(sku="SKU-EDIT-5", barcode="6667778889990", name="Товар 5", unit="шт")
    item2 = Nomenclature(sku="SKU-EDIT-6", barcode="7778889990001", name="Товар 6", unit="шт")
    db.session.add_all([item1, item2])
    db.session.commit()

    resp = client_logged_in.post(
        f"/nomenclature/{item2.id}/barcode", data={"barcode": "6667778889990"}, follow_redirects=True
    )

    assert "уже используется" in resp.get_data(as_text=True)
    assert Nomenclature.query.get(item2.id).barcode == "7778889990001"


def test_update_barcode_rejects_empty(db, client_logged_in):
    item = Nomenclature(sku="SKU-EDIT-7", barcode="8889990001112", name="Товар 7", unit="шт")
    db.session.add(item)
    db.session.commit()

    client_logged_in.post(f"/nomenclature/{item.id}/barcode", data={"barcode": "  "})

    assert Nomenclature.query.get(item.id).barcode == "8889990001112"


def test_update_name_blocked_without_permission(db, client):
    item = Nomenclature(sku="SKU-EDIT-8", barcode="9990001112223", name="Старое название", unit="шт")
    db.session.add(item)
    db.session.commit()

    user = _make_staff_user(nomenclature_edit_allowed=False)
    _login_as(client, user)

    client.post(f"/nomenclature/{item.id}/name", data={"name": "Новое название"})

    assert Nomenclature.query.get(item.id).name == "Старое название"


def test_update_name_allowed_with_permission(db, client_logged_in):
    item = Nomenclature(sku="SKU-EDIT-9", barcode="0001112223334", name="Старое название 2", unit="шт")
    db.session.add(item)
    db.session.commit()

    client_logged_in.post(f"/nomenclature/{item.id}/name", data={"name": "Новое название 2"})

    assert Nomenclature.query.get(item.id).name == "Новое название 2"


def test_update_sections_route_can_grant_and_revoke_edit_flag(db, client_logged_in):
    user = _make_staff_user(nomenclature_edit_allowed=False)

    client_logged_in.post(
        f"/users/{user.id}/sections",
        data={"mode": "full", "nomenclature_edit": "on"},
    )
    assert User.query.get(user.id).nomenclature_edit_allowed is True

    client_logged_in.post(f"/users/{user.id}/sections", data={"mode": "full"})
    assert User.query.get(user.id).nomenclature_edit_allowed is False


def test_list_nomenclature_hides_edit_controls_without_permission(db, client):
    user = _make_staff_user(nomenclature_edit_allowed=False)
    _login_as(client, user)

    html = client.get("/nomenclature/").get_data(as_text=True)

    assert "Добавить товар вручную" not in html
    assert "не разрешено" not in html  # просто скрыто, не флеш-ошибка


def test_list_nomenclature_shows_edit_controls_with_permission(db, client):
    user = _make_staff_user(nomenclature_edit_allowed=True)
    _login_as(client, user)

    html = client.get("/nomenclature/").get_data(as_text=True)

    assert "Добавить товар вручную" in html
