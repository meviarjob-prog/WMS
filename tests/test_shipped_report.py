"""Отчет «Отгружено по складам» (reports.shipped_report) — сколько и какого
товара реально отгружено (перемещение завершено) по складам назначения, с
фильтрами по дате и складу."""

from datetime import datetime, timedelta

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, Nomenclature, Warehouse


def _make_warehouse(code):
    wh = Warehouse(code=code, name=f"Склад {code}")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(barcode, name="Товар отгрузки"):
    item = Nomenclature(sku=barcode, barcode=barcode, name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _ship(client, sender, dest, item, qty, box_number):
    box = Box(box_number=box_number, warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
    db.session.commit()

    doc = MovementDocument(number=f"SHIP-{box_number}", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()
    client.post(f"/movement/{doc.id}/boxes/add", data={"box_number": box_number})
    client.post(f"/movement/{doc.id}/complete")
    return MovementDocument.query.get(doc.id)


def test_shipped_report_aggregates_qty_by_warehouse_and_item(db, client_logged_in):
    sender = _make_warehouse("WH-SHIP-A")
    dest = _make_warehouse("WH-SHIP-B")
    item = _make_item("9990000001")

    _ship(client_logged_in, sender, dest, item, 10, "BOX-SHIP-1")
    _ship(client_logged_in, sender, dest, item, 5, "BOX-SHIP-2")

    html = client_logged_in.get("/reports/shipped").get_data(as_text=True)

    assert "Товар отгрузки" in html
    assert "15" in html


def test_shipped_report_filters_by_warehouse(db, client_logged_in):
    sender = _make_warehouse("WH-SHIP-C")
    dest1 = _make_warehouse("WH-SHIP-D")
    dest2 = _make_warehouse("WH-SHIP-E")
    item = _make_item("9990000002")

    _ship(client_logged_in, sender, dest1, item, 7, "BOX-SHIP-3")
    _ship(client_logged_in, sender, dest2, item, 3, "BOX-SHIP-4")

    html = client_logged_in.get(f"/reports/shipped?warehouse_id={dest1.id}").get_data(as_text=True)

    assert "7" in html
    assert "<td>Склад WH-SHIP-D</td>" in html
    assert "<td>Склад WH-SHIP-E</td>" not in html


def test_shipped_report_filters_by_date_range(db, client_logged_in):
    sender = _make_warehouse("WH-SHIP-F")
    dest = _make_warehouse("WH-SHIP-G")
    item = _make_item("9990000003")

    doc = _ship(client_logged_in, sender, dest, item, 8, "BOX-SHIP-5")
    doc.completed_at = datetime.utcnow() - timedelta(days=10)
    db.session.commit()

    date_from = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    html = client_logged_in.get(f"/reports/shipped?date_from={date_from}").get_data(as_text=True)

    assert "За выбранный период отгрузок нет" in html


def test_shipped_report_excludes_draft_movements(db, client_logged_in):
    sender = _make_warehouse("WH-SHIP-H")
    dest = _make_warehouse("WH-SHIP-I")
    item = _make_item("9990000004")

    box = Box(box_number="BOX-SHIP-6", warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=99))
    doc = MovementDocument(number="SHIP-DRAFT-1", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()
    client_logged_in.post(f"/movement/{doc.id}/boxes/add", data={"box_number": box.box_number})
    # Черновик — не завершаем.

    html = client_logged_in.get("/reports/shipped").get_data(as_text=True)

    assert "За выбранный период отгрузок нет" in html


def test_shipped_report_excel_export_returns_xlsx(db, client_logged_in):
    sender = _make_warehouse("WH-SHIP-J")
    dest = _make_warehouse("WH-SHIP-K")
    item = _make_item("9990000005")
    _ship(client_logged_in, sender, dest, item, 4, "BOX-SHIP-7")

    resp = client_logged_in.get("/reports/shipped.xlsx")

    assert resp.status_code == 200
    assert resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
