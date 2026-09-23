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
