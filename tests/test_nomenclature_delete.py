"""Удаление одного товара из номенклатуры (nomenclature.delete_nomenclature)
— доступно только администратору, и только если по товару нет остатков и
документов (та же защита, что и в массовой очистке — clear_nomenclature),
иначе эти записи осиротеют (см. чат)."""

from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, User, Warehouse


def _make_staff_user(username="staffer-del"):
    user = User(username=username, full_name="Складской", role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    return user


def _login_as(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_admin_can_delete_unused_item(db, client_logged_in):
    item = Nomenclature(sku="SKU-DEL-1", barcode="1110001112223", name="Ненужный товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    item_id = item.id

    resp = client_logged_in.post(f"/nomenclature/{item_id}/delete", follow_redirects=True)

    assert "удален" in resp.get_data(as_text=True)
    assert Nomenclature.query.get(item_id) is None


def test_non_admin_cannot_delete_item(db, client):
    item = Nomenclature(sku="SKU-DEL-2", barcode="2220002223334", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    user = _make_staff_user()
    _login_as(client, user)

    resp = client.post(f"/nomenclature/{item.id}/delete", follow_redirects=True)

    assert "может только администратор" in resp.get_data(as_text=True)
    assert Nomenclature.query.get(item.id) is not None


def test_cannot_delete_item_with_stock_history(db, client_logged_in):
    wh = Warehouse(code="WH-DEL-1", name="Тест склад")
    db.session.add(wh)
    db.session.commit()
    item = Nomenclature(sku="SKU-DEL-3", barcode="3330003334445", name="Товар с историей", unit="шт")
    db.session.add(item)
    db.session.commit()
    box = Box(box_number="BOX-DEL-1", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=5))
    db.session.commit()

    resp = client_logged_in.post(f"/nomenclature/{item.id}/delete", follow_redirects=True)

    assert "Нельзя удалить" in resp.get_data(as_text=True)
    assert Nomenclature.query.get(item.id) is not None


def test_delete_button_shown_for_admin(db, client_logged_in):
    item = Nomenclature(sku="SKU-DEL-4", barcode="4440004445556", name="Товар для проверки кнопки", unit="шт")
    db.session.add(item)
    db.session.commit()

    html = client_logged_in.get("/nomenclature/").get_data(as_text=True)

    assert f'/nomenclature/{item.id}/delete' in html


def test_delete_button_hidden_for_non_admin(db, client):
    item = Nomenclature(sku="SKU-DEL-4B", barcode="4440004445557", name="Товар для проверки кнопки 2", unit="шт")
    db.session.add(item)
    user = _make_staff_user("staffer-nodelete")
    db.session.commit()

    _login_as(client, user)
    html = client.get("/nomenclature/").get_data(as_text=True)

    assert f'/nomenclature/{item.id}/delete' not in html


def test_delete_redirects_with_search_query_preserved(db, client_logged_in):
    item = Nomenclature(sku="SKU-DEL-5", barcode="5550005556667", name="Уникальный поисковый товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    resp = client_logged_in.post(
        f"/nomenclature/{item.id}/delete", data={"q": "Уникальный"}, follow_redirects=False
    )

    assert resp.status_code == 302
    assert "q=" in resp.headers["Location"]
