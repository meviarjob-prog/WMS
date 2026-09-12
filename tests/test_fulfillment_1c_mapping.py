"""Соответствие "склад-город WMS -> склад 1С" для склада-получателя при
выгрузке перемещений (см. wms/blueprints/warehouses.py:FULFILLMENT_1C_DEFAULTS
и integration_1c._to_warehouse_name_for_1c). Настраивается отдельно на
каждый склад-город (не на город целиком) — одна и та же площадка одного
города может ехать на разные склады 1С в зависимости от маркетплейса
(например, ВБ Краснодар — на СЦ, а ОЗОН Краснодар — на фулфилмент);
несколько РАЗНЫХ городов WMS могут при этом указывать на один и тот же
склад 1С (Черкесск и Пятигорск — оба к одному фулфилменту)."""

from wms.blueprints.shipment_plan import _get_or_create_city_warehouse
from wms.blueprints.warehouses import default_fulfillment_1c_name
from wms.extensions import db
from wms.models import AppSetting, Box, BoxItem, MovementDocument, MovementLine, Nomenclature, User, Warehouse

TOKEN = "test-1c-token"


def test_default_fulfillment_1c_name_known_city():
    assert default_fulfillment_1c_name("Казань") == "Товары в пути ФФ КАЗАНЬ, Взлётная 30"
    assert default_fulfillment_1c_name("питер") == "Товары в пути ФФ СПБ"


def test_default_fulfillment_1c_name_unknown_city_is_none():
    assert default_fulfillment_1c_name("Владивосток") is None


def test_cherkessk_and_pyatigorsk_share_same_1c_warehouse():
    assert (
        default_fulfillment_1c_name("Черкесск")
        == default_fulfillment_1c_name("Пятигорск")
        == "Товары в пути ФФ ЧЕРКЕССК (Лейла)"
    )


def test_new_city_warehouse_gets_default_1c_name_on_creation(db):
    wh = _get_or_create_city_warehouse("ozon", "Казань")
    assert wh.fulfillment_1c_name == "Товары в пути ФФ КАЗАНЬ, Взлётная 30"


def test_new_city_warehouse_unknown_city_has_no_default(db):
    wh = _get_or_create_city_warehouse("wb", "Владивосток")
    assert wh.fulfillment_1c_name is None


def test_update_fulfillment_1c_name_is_per_warehouse_not_per_city(db, client_logged_in):
    """Реальный кейс: ВБ Краснодар едет на СЦ, а ОЗОН Краснодар — на
    фулфилмент — один и тот же город, разные склады 1С в зависимости от
    площадки. Правка одного склада не должна трогать второй."""
    ozon = Warehouse(code="WH-M1", name="ОЗОН: Краснодар", marketplace="ozon", marketplace_city="Краснодар")
    wb = Warehouse(code="WH-M2", name="ВБ: Краснодар", marketplace="wb", marketplace_city="Краснодар")
    db.session.add_all([ozon, wb])
    db.session.commit()

    client_logged_in.post(
        f"/warehouses/{wb.id}/fulfillment-1c-name",
        data={"fulfillment_1c_name": "СЦ Краснодар"},
    )
    client_logged_in.post(
        f"/warehouses/{ozon.id}/fulfillment-1c-name",
        data={"fulfillment_1c_name": "Товары в пути ФФ КРАСНОДАР"},
    )

    assert Warehouse.query.get(wb.id).fulfillment_1c_name == "СЦ Краснодар"
    assert Warehouse.query.get(ozon.id).fulfillment_1c_name == "Товары в пути ФФ КРАСНОДАР"


def test_update_fulfillment_1c_name_does_not_touch_other_warehouses(db, client_logged_in):
    kazan = Warehouse(code="WH-M3", name="ОЗОН: Казань", marketplace="ozon", marketplace_city="Казань")
    moscow = Warehouse(code="WH-M4", name="ОЗОН: Москва", marketplace="ozon", marketplace_city="Москва")
    db.session.add_all([kazan, moscow])
    db.session.commit()

    client_logged_in.post(
        f"/warehouses/{kazan.id}/fulfillment-1c-name",
        data={"fulfillment_1c_name": "Товары в пути ФФ КАЗАНЬ, Взлётная 30"},
    )

    assert Warehouse.query.get(moscow.id).fulfillment_1c_name is None


def test_update_fulfillment_1c_name_requires_admin(db, client):
    wh = Warehouse(code="WH-M5", name="ОЗОН: Казань", marketplace="ozon", marketplace_city="Казань")
    db.session.add(wh)
    user = User(username="staff-1c", full_name="Сотрудник", role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True

    client.post(
        f"/warehouses/{wh.id}/fulfillment-1c-name",
        data={"fulfillment_1c_name": "Что-то"},
    )

    assert Warehouse.query.get(wh.id).fulfillment_1c_name is None


def _set_token():
    db.session.add(AppSetting(key="api_1c_token", value=TOKEN))
    db.session.commit()


def test_export_uses_configured_fulfillment_1c_name(db, client_logged_in):
    _set_token()
    sender = Warehouse(code="WH-M6", name="Основной склад")
    city = Warehouse(
        code="WH-M7",
        name="ОЗОН: Казань",
        marketplace="ozon",
        marketplace_city="Казань",
        fulfillment_1c_name="Товары в пути ФФ КАЗАНЬ, Взлётная 30",
    )
    db.session.add_all([sender, city])
    db.session.commit()

    item = Nomenclature(sku="9990000099", barcode="9990000099", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    box = Box(box_number="BOX-M1", warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=1))
    doc = MovementDocument(number="PER-M1", from_warehouse_id=sender.id, to_warehouse_id=city.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id, from_cell_id=box.cell_id)
    )
    db.session.commit()
    client_logged_in.post(f"/movement/{doc.id}/complete")

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()
    movement = next(m for m in data["movements"] if m["number"] == "PER-M1")
    assert movement["to_warehouse"] == "Товары в пути ФФ КАЗАНЬ, Взлётная 30"
