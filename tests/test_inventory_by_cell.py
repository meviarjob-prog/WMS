"""Выборочная инвентаризация по ячейке (см. чат): создание листа для одной
конкретной ячейки, сравнение только с содержимым этой ячейки (а не всего
склада), и автоматическое размещение короба в эту ячейку при сканировании —
без отдельного подтверждения, даже если короб был в другой ячейке."""

from wms.extensions import db
from wms.models import Box, BoxItem, Cell, InventoryDocument, Nomenclature, UnplacedStock, Warehouse


def _make_warehouse(code="WH-CELL-INV"):
    wh = Warehouse(code=code, name="Тест склад ячеечной инвентаризации")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_cell(warehouse, code):
    cell = Cell(warehouse_id=warehouse.id, code=code)
    db.session.add(cell)
    db.session.commit()
    return cell


def _make_item(barcode, name="Товар"):
    item = Nomenclature(sku=barcode, barcode=barcode, name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_box(warehouse, box_number, cell=None):
    box = Box(box_number=box_number, warehouse_id=warehouse.id, status="stored" if cell else "open",
              cell_id=cell.id if cell else None)
    db.session.add(box)
    db.session.commit()
    return box


def _new_cell_inventory(client, warehouse, cell_code):
    client.post(
        "/inventory/new",
        data={"warehouse_id": warehouse.id, "mode": "cell", "cell_code": cell_code},
    )
    return InventoryDocument.query.filter_by(warehouse_id=warehouse.id).order_by(
        InventoryDocument.id.desc()
    ).first()


def test_new_document_cell_mode_requires_cell_code(db, client_logged_in):
    wh = _make_warehouse("WH-CELL-1")
    resp = client_logged_in.post(
        "/inventory/new", data={"warehouse_id": wh.id, "mode": "cell", "cell_code": ""}, follow_redirects=True
    )
    assert "Укажите код ячейки" in resp.get_data(as_text=True)
    assert InventoryDocument.query.filter_by(warehouse_id=wh.id).count() == 0


def test_new_document_cell_mode_rejects_unknown_cell(db, client_logged_in):
    wh = _make_warehouse("WH-CELL-2")
    resp = client_logged_in.post(
        "/inventory/new",
        data={"warehouse_id": wh.id, "mode": "cell", "cell_code": "NOPE"},
        follow_redirects=True,
    )
    assert "не найдена" in resp.get_data(as_text=True)
    assert InventoryDocument.query.filter_by(warehouse_id=wh.id).count() == 0


def test_new_document_cell_mode_creates_scoped_document(db, client_logged_in):
    wh = _make_warehouse("WH-CELL-3")
    cell = _make_cell(wh, "A-01-01")
    doc = _new_cell_inventory(client_logged_in, wh, "A-01-01")
    assert doc is not None
    assert doc.cell_id == cell.id


def test_general_mode_still_creates_unscoped_document(db, client_logged_in):
    wh = _make_warehouse("WH-CELL-4")
    client_logged_in.post("/inventory/new", data={"warehouse_id": wh.id})
    doc = InventoryDocument.query.filter_by(warehouse_id=wh.id).order_by(InventoryDocument.id.desc()).first()
    assert doc is not None
    assert doc.cell_id is None


def test_cell_comparison_ignores_stock_in_other_cells(db, client_logged_in):
    """Сравнение для ячеечной инвентаризации берет только содержимое ЭТОЙ
    ячейки — товар в другой ячейке того же склада не должен считаться
    недостачей."""
    wh = _make_warehouse("WH-CELL-5")
    cell_a = _make_cell(wh, "A-01")
    cell_b = _make_cell(wh, "B-01")
    item_in_a = _make_item("8880000301", "Товар в ячейке A")
    item_in_b = _make_item("8880000302", "Товар в ячейке B")

    box_a = _make_box(wh, "BOX-CELL-A", cell=cell_a)
    db.session.add(BoxItem(box_id=box_a.id, nomenclature_id=item_in_a.id, qty=5))
    box_b = _make_box(wh, "BOX-CELL-B", cell=cell_b)
    db.session.add(BoxItem(box_id=box_b.id, nomenclature_id=item_in_b.id, qty=3))
    db.session.commit()

    doc = _new_cell_inventory(client_logged_in, wh, "A-01")
    client_logged_in.post(f"/inventory/{doc.id}/boxes/add", data={"box_number": "BOX-CELL-A"})

    html = client_logged_in.get(f"/inventory/{doc.id}").get_data(as_text=True)
    assert "Товар в ячейке A" in html
    assert "Товар в ячейке B" not in html


def test_cell_comparison_ignores_unplaced_stock(db, client_logged_in):
    """Неразмещенный остаток склада не привязан ни к какой ячейке — не
    должен попадать в сравнение для ячеечной инвентаризации."""
    wh = _make_warehouse("WH-CELL-6")
    cell = _make_cell(wh, "C-01")
    loose_item = _make_item("8880000303", "Неразмещенный товар")
    UnplacedStock.add(wh.id, loose_item.id, 20)
    db.session.commit()

    doc = _new_cell_inventory(client_logged_in, wh, "C-01")
    html = client_logged_in.get(f"/inventory/{doc.id}").get_data(as_text=True)
    assert "Неразмещенный товар" not in html


def test_scanning_box_in_cell_inventory_places_it_immediately(db, client_logged_in):
    """Короб, который сейчас нигде не размещен (открыт), при сканировании в
    ячеечной инвентаризации сразу же переставляется в эту ячейку — без
    подтверждения."""
    wh = _make_warehouse("WH-CELL-7")
    cell = _make_cell(wh, "D-01")
    item = _make_item("8880000304", "Товар для размещения")
    box = _make_box(wh, "BOX-CELL-D")
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=2))
    db.session.commit()

    doc = _new_cell_inventory(client_logged_in, wh, "D-01")
    client_logged_in.post(f"/inventory/{doc.id}/boxes/add", data={"box_number": "BOX-CELL-D"})

    db.session.refresh(box)
    assert box.cell_id == cell.id
    assert box.status == "stored"


def test_scanning_box_from_another_cell_moves_it_without_confirmation(db, client_logged_in):
    """Короб, уже стоящий в ДРУГОЙ ячейке, при сканировании в ячеечной
    инвентаризации переносится в новую ячейку сразу (см. AskUserQuestion:
    "Переносить сразу, без подтверждения")."""
    wh = _make_warehouse("WH-CELL-8")
    old_cell = _make_cell(wh, "E-01")
    new_cell = _make_cell(wh, "E-02")
    item = _make_item("8880000305", "Товар для переноса")
    box = _make_box(wh, "BOX-CELL-E", cell=old_cell)
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=1))
    db.session.commit()

    doc = _new_cell_inventory(client_logged_in, wh, "E-02")
    resp = client_logged_in.post(
        f"/inventory/{doc.id}/boxes/add", data={"box_number": "BOX-CELL-E"}, follow_redirects=True
    )

    db.session.refresh(box)
    assert box.cell_id == new_cell.id
    assert "перемещен из ячейки E-01" in resp.get_data(as_text=True)


def test_merge_rejects_documents_with_different_cells(db, client_logged_in):
    wh = _make_warehouse("WH-CELL-9")
    _make_cell(wh, "F-01")
    _make_cell(wh, "F-02")
    doc1 = _new_cell_inventory(client_logged_in, wh, "F-01")
    doc2 = _new_cell_inventory(client_logged_in, wh, "F-02")

    resp = client_logged_in.post(
        "/inventory/merge", data={"doc_ids": [doc1.id, doc2.id]}, follow_redirects=True
    )
    assert "разным ячейкам" in resp.get_data(as_text=True)
    db.session.refresh(doc1)
    db.session.refresh(doc2)
    assert doc1.status == "draft"
    assert doc2.status == "draft"


def test_merge_rejects_cell_document_with_general_document(db, client_logged_in):
    wh = _make_warehouse("WH-CELL-10")
    _make_cell(wh, "G-01")
    cell_doc = _new_cell_inventory(client_logged_in, wh, "G-01")
    client_logged_in.post("/inventory/new", data={"warehouse_id": wh.id})
    general_doc = InventoryDocument.query.filter_by(warehouse_id=wh.id, cell_id=None).first()

    resp = client_logged_in.post(
        "/inventory/merge", data={"doc_ids": [cell_doc.id, general_doc.id]}, follow_redirects=True
    )
    assert "разным ячейкам" in resp.get_data(as_text=True)


def test_merge_allows_two_documents_for_same_cell(db, client_logged_in):
    wh = _make_warehouse("WH-CELL-11")
    cell = _make_cell(wh, "H-01")
    doc1 = _new_cell_inventory(client_logged_in, wh, "H-01")
    doc2 = _new_cell_inventory(client_logged_in, wh, "H-01")

    resp = client_logged_in.post(
        "/inventory/merge", data={"doc_ids": [doc1.id, doc2.id]}, follow_redirects=True
    )
    merged = InventoryDocument.query.filter_by(warehouse_id=wh.id, cell_id=cell.id, status="draft").first()
    assert merged is not None
    assert merged.id not in (doc1.id, doc2.id)


def test_new_form_shows_mode_toggle(db, client_logged_in):
    wh = _make_warehouse("WH-CELL-12")
    html = client_logged_in.get("/inventory/new").get_data(as_text=True)
    assert "По ячейке" in html
    assert 'name="cell_code"' in html


def test_detail_shows_cell_code_in_header(db, client_logged_in):
    wh = _make_warehouse("WH-CELL-13")
    _make_cell(wh, "K-01")
    doc = _new_cell_inventory(client_logged_in, wh, "K-01")
    html = client_logged_in.get(f"/inventory/{doc.id}").get_data(as_text=True)
    assert "K-01" in html


def test_detail_hides_loose_stock_form_for_cell_document(db, client_logged_in):
    """"Учесть товар без короба" не имеет смысла для ячеечной
    инвентаризации — неразмещенный остаток ни к какой ячейке не привязан."""
    wh = _make_warehouse("WH-CELL-14")
    _make_cell(wh, "L-01")
    doc = _new_cell_inventory(client_logged_in, wh, "L-01")
    html = client_logged_in.get(f"/inventory/{doc.id}").get_data(as_text=True)
    assert "Учесть товар без короба" not in html


def test_detail_shows_loose_stock_form_for_general_document(db, client_logged_in):
    wh = _make_warehouse("WH-CELL-15")
    client_logged_in.post("/inventory/new", data={"warehouse_id": wh.id})
    doc = InventoryDocument.query.filter_by(warehouse_id=wh.id).order_by(InventoryDocument.id.desc()).first()
    html = client_logged_in.get(f"/inventory/{doc.id}").get_data(as_text=True)
    assert "Учесть товар без короба" in html
