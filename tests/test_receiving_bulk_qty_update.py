"""Массовое сохранение количества по всем строкам приемки одной кнопкой
(receiving.update_lines_bulk) — вместо перезагрузки страницы после правки
каждой отдельной строки (см. update_line)."""

from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, ReceivingDocument, ReceivingLine, Warehouse


def _make_warehouse(code="WH-BULK"):
    wh = Warehouse(code=code, name="Тест склад массового сохранения")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(barcode, name="Товар массового сохранения"):
    item = Nomenclature(sku=barcode, barcode=barcode, name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def test_bulk_update_saves_multiple_lines_at_once(db, client_logged_in):
    wh = _make_warehouse("WH-BULK-1")
    item1 = _make_item("5550000001", "Товар А")
    item2 = _make_item("5550000002", "Товар Б")
    doc = ReceivingDocument(number="BULK-0001", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()
    line1 = ReceivingLine(document_id=doc.id, nomenclature_id=item1.id, qty=10)
    line2 = ReceivingLine(document_id=doc.id, nomenclature_id=item2.id, qty=5)
    db.session.add_all([line1, line2])
    db.session.commit()

    client_logged_in.post(
        f"/receiving/{doc.id}/lines/update-bulk",
        data={f"qty_{line1.id}": "12", f"qty_{line2.id}": "5"},
    )

    assert ReceivingLine.query.get(line1.id).qty == 12
    assert ReceivingLine.query.get(line2.id).qty == 5  # без изменений — тоже ок


def test_bulk_update_syncs_box_item_qty(db, client_logged_in):
    """Строка, упакованная в короб при приемке, синхронизирует BoxItem.qty
    так же, как это делает построчное update_line."""
    wh = _make_warehouse("WH-BULK-2")
    item = _make_item("5550000003")
    box = Box(box_number="BOX-BULK-1", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=4))
    doc = ReceivingDocument(number="BULK-0002", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=4, box_id=box.id)
    db.session.add(line)
    db.session.commit()

    client_logged_in.post(
        f"/receiving/{doc.id}/lines/update-bulk", data={f"qty_{line.id}": "6"}
    )

    assert ReceivingLine.query.get(line.id).qty == 6
    assert BoxItem.query.filter_by(box_id=box.id, nomenclature_id=item.id).first().qty == 6


def test_bulk_update_ignores_invalid_and_missing_values(db, client_logged_in):
    wh = _make_warehouse("WH-BULK-3")
    item = _make_item("5550000004")
    doc = ReceivingDocument(number="BULK-0003", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10)
    db.session.add(line)
    db.session.commit()

    resp = client_logged_in.post(
        f"/receiving/{doc.id}/lines/update-bulk", data={f"qty_{line.id}": "не число"}
    )

    assert resp.status_code in (200, 302)
    assert ReceivingLine.query.get(line.id).qty == 10


def test_bulk_update_blocked_when_completed(db, client_logged_in):
    wh = _make_warehouse("WH-BULK-4")
    item = _make_item("5550000005")
    doc = ReceivingDocument(number="BULK-0004", warehouse_id=wh.id, status="completed")
    db.session.add(doc)
    db.session.commit()
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10)
    db.session.add(line)
    db.session.commit()

    client_logged_in.post(
        f"/receiving/{doc.id}/lines/update-bulk", data={f"qty_{line.id}": "999"}
    )

    assert ReceivingLine.query.get(line.id).qty == 10
