"""Выгрузка для 1С (/integrations/1c/api/export) и подтверждение приема
(/integrations/1c/api/export/confirm) — доступ по токену, маршрутизация
склада назначения перемещений на фиктивный "Товары в пути на Фулфилмент"
(кроме прямых перемещений между двумя настоящими физическими складами),
фильтр по галочке бухгалтера "внесено в 1С" и группировка возвратов
поставщику по приемке (см. receiving.complete/разбраковка)."""

import json

from wms.extensions import db
from wms.models import (
    AppSetting,
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    Nomenclature,
    ReceivingDocument,
    SupplierReturn,
    Warehouse,
)

TOKEN = "test-1c-token"


def _set_token():
    db.session.add(AppSetting(key="api_1c_token", value=TOKEN))
    db.session.commit()


def _make_item(barcode, name="Товар для 1С"):
    item = Nomenclature(sku=barcode, barcode=barcode, name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _ship_box(sender, receiver, item, qty, box_number, client, accounting_entered=False):
    box = Box(box_number=box_number, warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
    doc = MovementDocument(
        number=f"PER-{box_number}", from_warehouse_id=sender.id, to_warehouse_id=receiver.id
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id, from_cell_id=box.cell_id)
    )
    db.session.commit()
    client.post(f"/movement/{doc.id}/complete")
    doc = MovementDocument.query.get(doc.id)
    doc.received_at = doc.completed_at
    if accounting_entered:
        from datetime import datetime

        doc.accounting_entered_at = datetime.utcnow()
    db.session.commit()
    return doc


def test_export_requires_token(db, client):
    resp = client.get("/integrations/1c/api/export")
    assert resp.status_code == 401


def test_export_movement_uses_fulfillment_warehouse_by_default(db, client_logged_in):
    _set_token()
    sender = Warehouse(code="WH-1C-1", name="Основной склад")
    city = Warehouse(code="WH-1C-2", name="ОЗОН: Казань", marketplace="ozon", marketplace_city="Казань")
    db.session.add_all([sender, city])
    db.session.commit()
    item = _make_item("9990000001")

    _ship_box(sender, city, item, 5, "BOX-1C-1", client_logged_in)

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()
    movement = next(m for m in data["movements"] if m["from_warehouse"] == "Основной склад")
    assert movement["to_warehouse"] == "Товары в пути на Фулфилмент"
    assert movement["lines"][0]["barcode"] == "9990000001"
    assert movement["lines"][0]["name"] == "Товар для 1С"


def test_export_direct_transfer_between_known_warehouses_keeps_real_name(db, client_logged_in):
    _set_token()
    main = Warehouse(code="WH-1C-3", name="Основной склад")
    second = Warehouse(code="WH-1C-4", name="Склад №2 (Шоссейная 167)")
    db.session.add_all([main, second])
    db.session.commit()
    item = _make_item("9990000002")

    _ship_box(main, second, item, 3, "BOX-1C-2", client_logged_in)

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()
    movement = next(m for m in data["movements"] if m["from_warehouse"] == "Основной склад")
    assert movement["to_warehouse"] == "Склад №2 (Шоссейная 167)"


def test_export_excludes_movements_marked_entered_in_1c(db, client_logged_in):
    _set_token()
    sender = Warehouse(code="WH-1C-5", name="Основной склад")
    city = Warehouse(code="WH-1C-6", name="ОЗОН: Пермь", marketplace="ozon", marketplace_city="Пермь")
    db.session.add_all([sender, city])
    db.session.commit()
    item = _make_item("9990000003")

    _ship_box(sender, city, item, 2, "BOX-1C-3", client_logged_in, accounting_entered=True)

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()
    assert all(m["number"] != "PER-BOX-1C-3" for m in data["movements"])


def test_export_groups_supplier_returns_by_receiving_document(db, client_logged_in):
    _set_token()
    wh = Warehouse(code="WH-1C-7", name="Основной склад")
    db.session.add(wh)
    db.session.commit()
    item1 = _make_item("9990000004", "Товар А")
    item2 = _make_item("9990000005", "Товар Б")
    doc = ReceivingDocument(
        number="НАКЛ-777", warehouse_id=wh.id, supplier="ИП Тестов", invoice_file_name="накладная.xlsx"
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add_all(
        [
            SupplierReturn(
                warehouse_id=wh.id,
                nomenclature_id=item1.id,
                qty=3,
                receiving_document_id=doc.id,
                supplier_name="ИП Тестов",
                invoice_number="НАКЛ-777",
            ),
            SupplierReturn(
                warehouse_id=wh.id,
                nomenclature_id=item2.id,
                qty=1,
                receiving_document_id=doc.id,
                supplier_name="ИП Тестов",
                invoice_number="НАКЛ-777",
            ),
            # Без invoice_number (ручное списание либо приемка без импорта
            # накладной) — не должен попасть в выгрузку вообще.
            SupplierReturn(warehouse_id=wh.id, nomenclature_id=item1.id, qty=9),
        ]
    )
    db.session.commit()

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()

    assert len(data["supplier_returns"]) == 1
    ret_doc = data["supplier_returns"][0]
    assert ret_doc["id"] == doc.id
    assert ret_doc["invoice_number"] == "НАКЛ-777"
    assert ret_doc["supplier"] == "ИП Тестов"
    assert {line["qty"] for line in ret_doc["lines"]} == {3, 1}


def test_export_confirm_marks_everything_synced(db, client_logged_in):
    _set_token()
    wh = Warehouse(code="WH-1C-8", name="Основной склад")
    db.session.add(wh)
    db.session.commit()
    item = _make_item("9990000006")
    doc = ReceivingDocument(
        number="НАКЛ-888", warehouse_id=wh.id, supplier="ИП Тестов", invoice_file_name="накладная.xlsx"
    )
    db.session.add(doc)
    db.session.commit()
    ret = SupplierReturn(
        warehouse_id=wh.id,
        nomenclature_id=item.id,
        qty=2,
        receiving_document_id=doc.id,
        supplier_name="ИП Тестов",
        invoice_number="НАКЛ-888",
    )
    db.session.add(ret)
    db.session.commit()

    resp = client_logged_in.post(
        "/integrations/1c/api/export/confirm",
        data=json.dumps({"movement_ids": [], "inventory_ids": [], "supplier_return_ids": [doc.id]}),
        content_type="application/json",
        headers={"X-1C-Token": TOKEN},
    )
    assert resp.get_json()["confirmed"]["supplier_returns"] == 1
    assert SupplierReturn.query.get(ret.id).synced_to_1c_at is not None

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    assert resp.get_json()["supplier_returns"] == []
