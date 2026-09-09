"""Поиск короба по перемещениям: где именно он числится (см.
movement.find_box) — нужно в первую очередь, когда добавить короб в новое
перемещение не дает блокировка "уже в другом перемещении"."""

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, MovementLine, Nomenclature, Warehouse


def _make_box_in_movement():
    sender = Warehouse(code="WH-F1", name="Склад-отправитель")
    dest = Warehouse(code="WH-F2", name="ОЗОН: Омск")
    db.session.add_all([sender, dest])
    db.session.commit()

    item = Nomenclature(sku="SKU-F1", barcode="7770000201", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    box = Box(box_number="BOX-000910", warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=1))
    db.session.commit()

    doc = MovementDocument(number="PER-F1", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=box.warehouse_id, from_cell_id=box.cell_id)
    )
    db.session.commit()
    return box, doc


def test_find_box_shows_document(db, client_logged_in):
    box, doc = _make_box_in_movement()

    resp = client_logged_in.get(f"/movement/find-box?box_number={box.box_number}")
    html = resp.get_data(as_text=True)

    assert doc.number in html
    assert "Черновик" in html


def test_find_box_not_in_any_movement(db, client_logged_in):
    sender = Warehouse(code="WH-F3", name="Свободный склад")
    db.session.add(sender)
    db.session.commit()
    box = Box(box_number="BOX-000920", warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()

    resp = client_logged_in.get(f"/movement/find-box?box_number={box.box_number}")
    html = resp.get_data(as_text=True)

    assert "ни в одном перемещении не числится" in html


def test_find_box_not_found(db, client_logged_in):
    resp = client_logged_in.get("/movement/find-box?box_number=BOX-999999")
    html = resp.get_data(as_text=True)
    assert "не найден" in html
