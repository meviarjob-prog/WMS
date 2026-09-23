"""Выборочная инвентаризация по РЯДУ целиком, без ячеек (см. чат —
помещения, где нет возможности завести ячейки): аналог инвентаризации по
ячейке (test_inventory_by_cell.py), только сравнение идет с тем, что стоит
в ряду напрямую (Box.zone_id), а не в конкретной ячейке."""

from wms.extensions import db
from wms.models import Box, BoxItem, InventoryDocument, Nomenclature, UnplacedStock, Warehouse, Zone


def _make_warehouse(code="WH-ZONE-INV"):
    wh = Warehouse(code=code, name="Тест склад рядовой инвентаризации")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_zone(warehouse, code):
    zone = Zone(warehouse_id=warehouse.id, code=code)
    db.session.add(zone)
    db.session.commit()
    return zone


def _make_item(barcode, name="Товар"):
    item = Nomenclature(sku=barcode, barcode=barcode, name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_box(warehouse, box_number, zone=None):
    box = Box(
        box_number=box_number,
        warehouse_id=warehouse.id,
        status="stored" if zone else "open",
        zone_id=zone.id if zone else None,
    )
    db.session.add(box)
    db.session.commit()
    return box


def _new_zone_inventory(client, warehouse, zone_code):
    client.post(
        "/inventory/new",
        data={"warehouse_id": warehouse.id, "mode": "zone", "cell_code": zone_code},
    )
    return InventoryDocument.query.filter_by(warehouse_id=warehouse.id).order_by(
        InventoryDocument.id.desc()
    ).first()


def test_new_document_zone_mode_requires_zone_code(db, client_logged_in):
    wh = _make_warehouse("WH-ZONE-1")
    resp = client_logged_in.post(
        "/inventory/new", data={"warehouse_id": wh.id, "mode": "zone", "cell_code": ""}, follow_redirects=True
    )
    assert "Укажите код ряда" in resp.get_data(as_text=True)
    assert InventoryDocument.query.filter_by(warehouse_id=wh.id).count() == 0


def test_new_document_zone_mode_rejects_unknown_zone(db, client_logged_in):
    wh = _make_warehouse("WH-ZONE-2")
    resp = client_logged_in.post(
        "/inventory/new",
        data={"warehouse_id": wh.id, "mode": "zone", "cell_code": "NOPE"},
        follow_redirects=True,
    )
    assert "не найден" in resp.get_data(as_text=True)
    assert InventoryDocument.query.filter_by(warehouse_id=wh.id).count() == 0


def test_new_document_zone_mode_creates_scoped_document(db, client_logged_in):
    wh = _make_warehouse("WH-ZONE-3")
    zone = _make_zone(wh, "ROW-01")
    doc = _new_zone_inventory(client_logged_in, wh, "ROW-01")
    assert doc is not None
    assert doc.zone_id == zone.id
    assert doc.cell_id is None


def test_zone_comparison_ignores_stock_in_other_rows(db, client_logged_in):
    """Сравнение для рядовой инвентаризации берет только то, что стоит в
    ЭТОМ ряду напрямую — товар в другом ряду того же склада не должен
    считаться недостачей."""
    wh = _make_warehouse("WH-ZONE-4")
    zone_a = _make_zone(wh, "ROW-A")
    zone_b = _make_zone(wh, "ROW-B")
    item_in_a = _make_item("8880000401", "Товар в ряду A")
    item_in_b = _make_item("8880000402", "Товар в ряду B")

    box_a = _make_box(wh, "BOX-ZONE-A", zone=zone_a)
    db.session.add(BoxItem(box_id=box_a.id, nomenclature_id=item_in_a.id, qty=5))
    box_b = _make_box(wh, "BOX-ZONE-B", zone=zone_b)
    db.session.add(BoxItem(box_id=box_b.id, nomenclature_id=item_in_b.id, qty=3))
    db.session.commit()

    doc = _new_zone_inventory(client_logged_in, wh, "ROW-A")
    client_logged_in.post(f"/inventory/{doc.id}/boxes/add", data={"box_number": "BOX-ZONE-A"})

    html = client_logged_in.get(f"/inventory/{doc.id}").get_data(as_text=True)
    assert "Товар в ряду A" in html
    assert "Товар в ряду B" not in html


def test_zone_comparison_ignores_unplaced_stock(db, client_logged_in):
    wh = _make_warehouse("WH-ZONE-5")
    _make_zone(wh, "ROW-C")
    loose_item = _make_item("8880000403", "Неразмещенный товар")
    UnplacedStock.add(wh.id, loose_item.id, 20)
    db.session.commit()

    doc = _new_zone_inventory(client_logged_in, wh, "ROW-C")
    html = client_logged_in.get(f"/inventory/{doc.id}").get_data(as_text=True)
    assert "Неразмещенный товар" not in html


def test_scanning_box_in_zone_inventory_places_it_immediately(db, client_logged_in):
    wh = _make_warehouse("WH-ZONE-6")
    zone = _make_zone(wh, "ROW-D")
    item = _make_item("8880000404", "Товар для размещения")
    box = _make_box(wh, "BOX-ZONE-D")
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=2))
    db.session.commit()

    doc = _new_zone_inventory(client_logged_in, wh, "ROW-D")
    client_logged_in.post(f"/inventory/{doc.id}/boxes/add", data={"box_number": "BOX-ZONE-D"})

    db.session.refresh(box)
    assert box.zone_id == zone.id
    assert box.cell_id is None
    assert box.status == "stored"


def test_scanning_box_from_a_cell_moves_it_into_the_row_without_confirmation(db, client_logged_in):
    """Короб, стоявший в обычной ячейке, при сканировании в рядовой
    инвентаризации переносится напрямую в ряд (аналог переноса между
    ячейками в test_inventory_by_cell.py)."""
    from wms.models import Cell

    wh = _make_warehouse("WH-ZONE-7")
    old_cell = Cell(warehouse_id=wh.id, code="OLD-01")
    db.session.add(old_cell)
    db.session.commit()
    zone = _make_zone(wh, "ROW-E")
    item = _make_item("8880000405", "Товар для переноса")
    box = Box(box_number="BOX-ZONE-E", warehouse_id=wh.id, status="stored", cell_id=old_cell.id)
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=1))
    db.session.commit()

    doc = _new_zone_inventory(client_logged_in, wh, "ROW-E")
    resp = client_logged_in.post(
        f"/inventory/{doc.id}/boxes/add", data={"box_number": "BOX-ZONE-E"}, follow_redirects=True
    )

    db.session.refresh(box)
    assert box.zone_id == zone.id
    assert box.cell_id is None
    assert "перемещен из ячейки OLD-01" in resp.get_data(as_text=True)


def test_merge_rejects_documents_with_different_rows(db, client_logged_in):
    wh = _make_warehouse("WH-ZONE-8")
    _make_zone(wh, "ROW-F")
    _make_zone(wh, "ROW-G")
    doc1 = _new_zone_inventory(client_logged_in, wh, "ROW-F")
    doc2 = _new_zone_inventory(client_logged_in, wh, "ROW-G")

    resp = client_logged_in.post(
        "/inventory/merge", data={"doc_ids": [doc1.id, doc2.id]}, follow_redirects=True
    )
    assert "разным ячейкам/рядам" in resp.get_data(as_text=True)
    db.session.refresh(doc1)
    db.session.refresh(doc2)
    assert doc1.status == "draft"
    assert doc2.status == "draft"


def test_merge_allows_two_documents_for_same_row(db, client_logged_in):
    wh = _make_warehouse("WH-ZONE-9")
    zone = _make_zone(wh, "ROW-H")
    doc1 = _new_zone_inventory(client_logged_in, wh, "ROW-H")
    doc2 = _new_zone_inventory(client_logged_in, wh, "ROW-H")

    client_logged_in.post(
        "/inventory/merge", data={"doc_ids": [doc1.id, doc2.id]}, follow_redirects=True
    )
    merged = InventoryDocument.query.filter_by(warehouse_id=wh.id, zone_id=zone.id, status="draft").first()
    assert merged is not None
    assert merged.id not in (doc1.id, doc2.id)


def test_new_form_shows_zone_mode_toggle(db, client_logged_in):
    html = client_logged_in.get("/inventory/new").get_data(as_text=True)
    assert "По ряду" in html


def test_detail_shows_row_code_in_header(db, client_logged_in):
    wh = _make_warehouse("WH-ZONE-10")
    _make_zone(wh, "ROW-I")
    doc = _new_zone_inventory(client_logged_in, wh, "ROW-I")
    html = client_logged_in.get(f"/inventory/{doc.id}").get_data(as_text=True)
    assert "ROW-I" in html


def test_detail_hides_loose_stock_form_for_row_document(db, client_logged_in):
    wh = _make_warehouse("WH-ZONE-11")
    _make_zone(wh, "ROW-J")
    doc = _new_zone_inventory(client_logged_in, wh, "ROW-J")
    html = client_logged_in.get(f"/inventory/{doc.id}").get_data(as_text=True)
    assert "Учесть товар без короба" not in html
