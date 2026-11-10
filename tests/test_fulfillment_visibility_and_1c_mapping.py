"""Фулфилмент видит свой склад (короба), печатает стикеры только своих
коробов, видит общую номенклатуру (остаток — только своего склада), а при
приемке ему доступны подсказки по ВСЕЙ номенклатуре (см. чат). Плюс
сопоставление собственных складов с названием склада в 1С: выгрузка
перемещения между собственными складами берет его (чтобы в 1С подтягивался
нужный склад)."""

from datetime import datetime

from wms.blueprints.integration_1c import _movement_payload, _to_warehouse_name_for_1c
from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    Nomenclature,
    UnplacedStock,
    User,
    Warehouse,
)


def _wh(code, name, **kw):
    wh = Warehouse(code=code, name=name, **kw)
    db.session.add(wh)
    db.session.commit()
    return wh


def _ff_user(warehouse, username="ff-vis"):
    user = User(username=username, role="fulfillment", warehouse_id=warehouse.id)
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _box(wh, number, item=None, qty=5):
    box = Box(box_number=number, warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    if item:
        db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
        db.session.commit()
    return box


def _item(sku, barcode, name):
    item = Nomenclature(sku=sku, barcode=barcode, name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


# ---------- Фулфилмент: короба и стикеры ----------


def test_fulfillment_boxes_list_shows_only_own_warehouse(db, client):
    own = _wh("WH-V1", "Склад ФФ")
    other = _wh("WH-V2", "Чужой склад")
    _box(own, "BOX-OWN-1")
    _box(other, "BOX-OTHER-1")
    _login(client, _ff_user(own))

    html = client.get("/boxes/").get_data(as_text=True)
    assert "BOX-OWN-1" in html
    assert "BOX-OTHER-1" not in html

    # warehouse_id из адресной строки не расширяет видимость
    html = client.get(f"/boxes/?warehouse_id={other.id}").get_data(as_text=True)
    assert "BOX-OTHER-1" not in html
    assert "BOX-OWN-1" in html


def test_fulfillment_can_open_and_print_own_box_but_not_foreign(db, client):
    own = _wh("WH-V3", "Склад ФФ")
    other = _wh("WH-V4", "Чужой склад")
    mine = _box(own, "BOX-MINE", _item("SKU-V1", "6660001001", "Товар"))
    foreign = _box(other, "BOX-FOREIGN")
    _login(client, _ff_user(own, "ff-print"))

    assert client.get(f"/boxes/{mine.id}").status_code == 200
    assert client.get(f"/labels/box/{mine.id}").status_code == 200
    assert client.get(f"/labels/box/{mine.id}.pdf").status_code == 200
    assert client.get(f"/labels/box/{mine.id}/items").status_code == 200

    assert client.get(f"/boxes/{foreign.id}").status_code == 404
    assert client.get(f"/labels/box/{foreign.id}").status_code == 404
    assert client.get(f"/labels/box/{foreign.id}.pdf").status_code == 404


def test_fulfillment_batch_label_pdf_skips_foreign_boxes(db, client):
    own = _wh("WH-V5", "Склад ФФ")
    other = _wh("WH-V6", "Чужой склад")
    mine = _box(own, "BOX-B-MINE")
    foreign = _box(other, "BOX-B-FOREIGN")
    _login(client, _ff_user(own, "ff-batch"))

    ok = client.get(f"/labels/boxes/batch.pdf?ids={mine.id},{foreign.id}")
    assert ok.status_code == 200  # свой короб напечатался

    only_foreign = client.get(f"/labels/boxes/batch.pdf?ids={foreign.id}")
    assert only_foreign.status_code == 404


def test_fulfillment_cannot_edit_box_contents(db, client):
    own = _wh("WH-V7", "Склад ФФ")
    box = _box(own, "BOX-NOEDIT", _item("SKU-V2", "6660001002", "Товар"))
    _login(client, _ff_user(own, "ff-noedit"))

    resp = client.post(f"/boxes/{box.id}/items/add", data={"nomenclature_id": 1, "qty": 3})
    assert resp.status_code == 302  # закрыто before_request, ничего не добавилось
    assert BoxItem.query.filter_by(box_id=box.id).count() == 1


# ---------- Фулфилмент: общая номенклатура и подсказки при приемке ----------


def test_fulfillment_sees_common_nomenclature_with_own_stock_only(db, client):
    own = _wh("WH-V8", "Основной")
    other = _wh("WH-V9", "Склад №2")
    item = _item("SKU-V3", "6660001003", "Общий товар каталога")
    UnplacedStock.add(own.id, item.id, 7)
    UnplacedStock.add(other.id, item.id, 99)
    db.session.commit()
    _login(client, _ff_user(own, "ff-nom"))

    html = client.get("/nomenclature/").get_data(as_text=True)
    assert "Общий товар каталога" in html
    assert ">7 шт<" in html or "7 шт" in html
    assert "Склад №2" not in html  # чужой склад в колонки не попадает
    assert "99 шт" not in html
    # ничего редактируемого
    assert 'name="sku"' not in html
    assert "Импорт из Excel" not in html


def test_fulfillment_cannot_edit_nomenclature_even_with_default_flag(db):
    wh = _wh("WH-V10", "Склад ФФ")
    user = _ff_user(wh, "ff-edit-flag")
    assert user.nomenclature_edit_allowed is not False  # по умолчанию True
    assert user.can_edit_nomenclature() is False


def test_fulfillment_gets_whole_nomenclature_in_receiving_lookups(db, client):
    own = _wh("WH-V11", "Склад ФФ")
    _item("SKU-V4", "6660001004", "Кардиган редкий для подсказки")
    _login(client, _ff_user(own, "ff-api"))

    found = client.get("/api/nomenclature/search?q=редкий")
    assert found.status_code == 200
    assert found.get_json()[0]["name"] == "Кардиган редкий для подсказки"

    by_code = client.get("/api/nomenclature/by-barcode/6660001004")
    assert by_code.status_code == 200 and by_code.get_json()["found"] is True


def test_fulfillment_api_hides_foreign_boxes(db, client):
    own = _wh("WH-V12", "Склад ФФ")
    other = _wh("WH-V13", "Чужой склад")
    _box(own, "BOX-API-OWN")
    _box(other, "BOX-API-OTHER")
    _login(client, _ff_user(own, "ff-api-box"))

    assert client.get("/api/box/by-number/BOX-API-OWN").status_code == 200
    assert client.get("/api/box/by-number/BOX-API-OTHER").status_code == 404


def test_fulfillment_still_blocked_from_cells_api_and_warehouses(db, client):
    own = _wh("WH-V14", "Склад ФФ")
    _login(client, _ff_user(own, "ff-closed"))

    for path in ("/api/cells/search?q=A", "/warehouses/", "/placement/"):
        assert client.get(path).status_code == 302


# ---------- 1С: сопоставление собственных складов ----------


def _doc(from_wh, to_wh):
    item = _item(f"SKU-1C-{from_wh.code}-{to_wh.code}", f"7770{from_wh.id:03d}{to_wh.id:03d}", "Товар")
    box = _box(from_wh, f"BOX-1C-{from_wh.id}-{to_wh.id}", item)
    doc = MovementDocument(
        number=f"PER-1C-{from_wh.id}-{to_wh.id}", from_warehouse_id=from_wh.id,
        to_warehouse_id=to_wh.id, status="completed", completed_at=datetime.utcnow(),
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=from_wh.id))
    db.session.commit()
    return doc


def test_own_to_own_transfer_uses_mapped_1c_name_for_destination_and_sender(db):
    main = _wh("WH-M1", "Основной склад", fulfillment_1c_name="Склад Основной (1С)")
    second = _wh("WH-M2", "Склад №2 (Шоссейная 167)", fulfillment_1c_name="Склад №2 в 1С")
    doc = _doc(main, second)

    payload = _movement_payload(doc)
    assert payload["to_warehouse"] == "Склад №2 в 1С"
    assert payload["from_warehouse"] == "Склад Основной (1С)"


def test_unmapped_own_to_own_keeps_wms_names(db):
    main = _wh("WH-M3", "Основной склад")
    second = _wh("WH-M4", "Склад №2 (Шоссейная 167)")
    doc = _doc(main, second)

    payload = _movement_payload(doc)
    assert payload["to_warehouse"] == "Склад №2 (Шоссейная 167)"
    assert payload["from_warehouse"] == "Основной склад"


def test_own_warehouse_outside_direct_names_uses_mapping_instead_of_transit(db):
    main = _wh("WH-M5", "Основной склад")
    workshop = _wh("WH-M6", "Склад №3", fulfillment_1c_name="Товары в пути ФФ Цех")
    doc = _doc(main, workshop)
    assert _to_warehouse_name_for_1c(doc) == "Товары в пути ФФ Цех"


def test_mapping_page_lists_own_warehouses_and_saves_name(db, client_logged_in):
    main = _wh("WH-M7", "Основной склад")
    city = _wh("WH-M8", "ОЗОН: Казань", marketplace="ozon", marketplace_city="Казань")

    html = client_logged_in.get("/warehouses/").get_data(as_text=True)
    assert "Свой склад" in html
    assert f"/warehouses/{main.id}/fulfillment-1c-name" in html
    assert f"/warehouses/{city.id}/fulfillment-1c-name" in html

    client_logged_in.post(f"/warehouses/{main.id}/fulfillment-1c-name", data={"fulfillment_1c_name": "Основной 1С"})
    assert Warehouse.query.get(main.id).fulfillment_1c_name == "Основной 1С"
