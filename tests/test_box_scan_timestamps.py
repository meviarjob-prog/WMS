"""Box.last_scanned_at/last_scanned_by_id (см. Box.mark_scanned) — время и
автор последнего скана короба, обновляется в любой операции: приемка,
размещение (выбор короба и расстановка по ячейке), перемещение, инвентаризация.
MovementLine.scanned_at — отдельно, привязано именно к этой строке
перемещения (не перезаписывается более поздними операциями)."""

from wms.extensions import db
from wms.models import (
    Box,
    Cell,
    InventoryDocument,
    MovementDocument,
    PlacementDocument,
    ReceivingDocument,
    Warehouse,
    Zone,
)


def _make_warehouse(code="WH-SCAN"):
    wh = Warehouse(code=code, name="Тест склад скана")
    db.session.add(wh)
    db.session.commit()
    return wh


def test_box_starts_without_scan_info(db):
    wh = _make_warehouse()
    box = Box(box_number="BOX-SCAN-1", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()

    assert box.last_scanned_at is None
    assert box.last_scanned_by is None


def test_receiving_select_box_marks_scanned(db, client_logged_in, admin_user):
    wh = _make_warehouse("WH-SCAN-2")
    box = Box(box_number="BOX-SCAN-2", warehouse_id=wh.id, status="open")
    db.session.add(box)
    doc = ReceivingDocument(number="REC-SCAN-1", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/boxes/select", data={"box_number": box.box_number})

    updated = Box.query.get(box.id)
    assert updated.last_scanned_at is not None
    assert updated.last_scanned_by_id == admin_user.id


def test_placement_select_box_marks_scanned(db, client_logged_in):
    wh = _make_warehouse("WH-SCAN-3")
    box = Box(box_number="BOX-SCAN-3", warehouse_id=wh.id, status="open")
    db.session.add(box)
    doc = PlacementDocument(number="RAZ-SCAN-1", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()

    client_logged_in.post(f"/placement/{doc.id}/boxes/select", data={"box_number": box.box_number})

    assert Box.query.get(box.id).last_scanned_at is not None


def test_placement_place_box_marks_scanned(db, client_logged_in):
    wh = _make_warehouse("WH-SCAN-4")
    zone = Zone(warehouse_id=wh.id, code="Z1", name="Зона 1")
    db.session.add(zone)
    db.session.commit()
    cell = Cell(warehouse_id=wh.id, zone_id=zone.id, code="Z1-01")
    box = Box(box_number="BOX-SCAN-4", warehouse_id=wh.id, status="open")
    db.session.add_all([cell, box])
    db.session.commit()

    client_logged_in.post(f"/placement/box/{box.id}/place", data={"cell_code": cell.code})

    assert Box.query.get(box.id).last_scanned_at is not None


def test_movement_add_box_marks_scanned_and_sets_line_scanned_at(db, client_logged_in):
    sender = _make_warehouse("WH-SCAN-5A")
    dest = _make_warehouse("WH-SCAN-5B")
    box = Box(box_number="BOX-SCAN-5", warehouse_id=sender.id, status="open")
    db.session.add(box)
    doc = MovementDocument(number="PER-SCAN-1", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()

    client_logged_in.post(f"/movement/{doc.id}/boxes/add", data={"box_number": box.box_number})

    updated = Box.query.get(box.id)
    assert updated.last_scanned_at is not None
    line = doc.lines.first()
    assert line.scanned_at is not None


def test_inventory_scan_box_marks_scanned(db, client_logged_in):
    wh = _make_warehouse("WH-SCAN-6")
    box = Box(box_number="BOX-SCAN-6", warehouse_id=wh.id, status="open")
    db.session.add(box)
    doc = InventoryDocument(number="INVT-SCAN-1", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()

    client_logged_in.post(f"/inventory/{doc.id}/boxes/add", data={"box_number": box.box_number})

    assert Box.query.get(box.id).last_scanned_at is not None


def test_box_detail_page_shows_last_scan_info(db, client_logged_in, admin_user):
    wh = _make_warehouse("WH-SCAN-7")
    box = Box(box_number="BOX-SCAN-7", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    box.mark_scanned(admin_user)
    db.session.commit()

    html = client_logged_in.get(f"/boxes/{box.id}").get_data(as_text=True)

    assert "Последнее сканирование" in html
    assert admin_user.display_name() in html
