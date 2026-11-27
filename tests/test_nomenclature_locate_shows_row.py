"""«Где товар» (nomenclature.locate) должен показывать ряд для коробов,
расставленных напрямую в ряду без ячейки (см. чат — помещения, где нет
возможности завести ячейки)."""

from wms.extensions import db
from wms.models import Box, BoxItem, Cell, Nomenclature, Warehouse, Zone


def _make_warehouse(code):
    wh = Warehouse(code=code, name="Склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(barcode):
    item = Nomenclature(sku=barcode, barcode=barcode, name="Товар для поиска", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def test_locate_shows_row_for_box_placed_directly_in_a_row(db, client_logged_in):
    wh = _make_warehouse("WH-LOCATE-1")
    zone = Zone(warehouse_id=wh.id, code="ROW-Z")
    db.session.add(zone)
    db.session.commit()
    item = _make_item("8880000501")
    box = Box(box_number="BOX-LOCATE-1", warehouse_id=wh.id, status="stored", zone_id=zone.id)
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=3))
    db.session.commit()

    html = client_logged_in.get(f"/nomenclature/locate?barcode={item.barcode}").get_data(as_text=True)

    assert "ряд ROW-Z" in html
    assert "не расставлен" not in html


def test_locate_still_shows_cell_for_box_placed_in_a_cell(db, client_logged_in):
    wh = _make_warehouse("WH-LOCATE-2")
    cell = Cell(warehouse_id=wh.id, code="C-99")
    db.session.add(cell)
    db.session.commit()
    item = _make_item("8880000502")
    box = Box(box_number="BOX-LOCATE-2", warehouse_id=wh.id, status="stored", cell_id=cell.id)
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=1))
    db.session.commit()

    html = client_logged_in.get(f"/nomenclature/locate?barcode={item.barcode}").get_data(as_text=True)

    assert "C-99" in html


def test_locate_shows_not_placed_for_box_with_no_location(db, client_logged_in):
    wh = _make_warehouse("WH-LOCATE-3")
    item = _make_item("8880000503")
    box = Box(box_number="BOX-LOCATE-3", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=1))
    db.session.commit()

    html = client_logged_in.get(f"/nomenclature/locate?barcode={item.barcode}").get_data(as_text=True)

    assert "не расставлен" in html


def test_locate_shows_warehouse_arrived_date(db, client_logged_in):
    """«В коробах» показывает, когда короб поступил на текущий склад (см.
    чат: "добавим дату добавления на склад")."""
    from datetime import datetime

    from wms.utils.timezone import to_moscow

    wh = _make_warehouse("WH-LOCATE-4")
    item = _make_item("8880000504")
    box = Box(box_number="BOX-LOCATE-4", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    box.warehouse_arrived_at = datetime(2026, 3, 15, 10, 0)
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=1))
    db.session.commit()

    html = client_logged_in.get(f"/nomenclature/locate?barcode={item.barcode}").get_data(as_text=True)

    expected = to_moscow(box.warehouse_arrived_at).strftime("%d.%m.%Y")
    assert expected in html


def test_box_warehouse_arrived_at_updates_on_movement_completion(db, client_logged_in):
    """Дата поступления на склад переезжает вместе с коробом — обновляется,
    когда перемещение завершается и короб переходит на склад назначения."""
    from datetime import datetime, timedelta

    from wms.models import MovementDocument, MovementLine, Nomenclature as Nom

    sender = Warehouse(code="WH-LOCATE-5A", name="Отправитель")
    dest = Warehouse(code="WH-LOCATE-5B", name="Получатель")
    db.session.add_all([sender, dest])
    db.session.commit()
    item = Nom(sku="SKU-LOCATE-5", barcode="8880000505", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    box = Box(box_number="BOX-LOCATE-5", warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    old_arrived = datetime.utcnow() - timedelta(days=30)
    box.warehouse_arrived_at = old_arrived
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=1))
    doc = MovementDocument(number="PER-LOCATE-5", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
    db.session.commit()

    client_logged_in.post(f"/movement/{doc.id}/complete")

    db.session.refresh(box)
    assert box.warehouse_id == dest.id
    assert box.warehouse_arrived_at > old_arrived
