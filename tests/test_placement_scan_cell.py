"""placement.scan_cell — обратный порядок относительно scan_box: сначала
выбирают склад и фиксируют ЯЧЕЙКУ, затем сканируют в нее короба один за
другим без повторного ввода ячейки (см. чат: "сканят ячейку — затем нужные
короба — завершить размещение — сканят другую ячейку"). Код ячейки уникален
только в пределах склада, поэтому склад выбирается явно."""

from wms.extensions import db
from wms.models import Box, BoxItem, Cell, Nomenclature, Warehouse, Zone


def _make_warehouse(suffix):
    wh = Warehouse(code=f"WH-SCANCELL{suffix}", name="Склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(suffix):
    item = Nomenclature(sku=f"SKU-SCANCELL{suffix}", barcode=f"77706000{suffix}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_box(warehouse, item, box_number, qty=1):
    box = Box(box_number=box_number, warehouse_id=warehouse.id, status="open")
    db.session.add(box)
    db.session.commit()
    if item is not None:
        db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
        db.session.commit()
    return box


def test_no_warehouse_shows_warehouse_picker(db, client_logged_in):
    warehouse = _make_warehouse("1")

    html = client_logged_in.get("/placement/scan-cell").get_data(as_text=True)

    assert "Выберите склад" in html
    assert warehouse.name in html


def test_warehouse_selected_shows_cell_input(db, client_logged_in):
    warehouse = _make_warehouse("2")

    html = client_logged_in.get(f"/placement/scan-cell?warehouse_id={warehouse.id}").get_data(as_text=True)

    assert warehouse.name in html
    assert 'name="cell_code"' in html


def test_unknown_cell_code_shows_not_found(db, client_logged_in):
    """Код может оказаться ни ячейкой, ни рядом (см. чат — ряд без ячеек) —
    сообщение теперь охватывает оба варианта."""
    warehouse = _make_warehouse("3")

    html = client_logged_in.get(
        f"/placement/scan-cell?warehouse_id={warehouse.id}&cell_code=NOPE"
    ).get_data(as_text=True)

    assert "не найдены" in html


def test_valid_cell_shows_box_scan_form_and_existing_boxes(db, client_logged_in):
    warehouse = _make_warehouse("4")
    item = _make_item("4")
    cell = Cell(warehouse_id=warehouse.id, code="B-01")
    db.session.add(cell)
    db.session.commit()
    existing_box = _make_box(warehouse, item, "BOX-SCANCELL-4A", qty=1)
    existing_box.cell_id = cell.id
    db.session.commit()

    html = client_logged_in.get(
        f"/placement/scan-cell?warehouse_id={warehouse.id}&cell_code=B-01"
    ).get_data(as_text=True)

    assert "B-01" in html
    assert existing_box.box_number in html
    assert 'name="box_number"' in html


def test_add_box_places_it_in_the_fixed_cell(db, client_logged_in):
    warehouse = _make_warehouse("5")
    item = _make_item("5")
    cell = Cell(warehouse_id=warehouse.id, code="B-02")
    db.session.add(cell)
    db.session.commit()
    box = _make_box(warehouse, item, "BOX-SCANCELL-5", qty=2)

    resp = client_logged_in.post(
        "/placement/scan-cell/add-box",
        data={"warehouse_id": warehouse.id, "cell_code": "B-02", "box_number": box.box_number},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert Box.query.get(box.id).cell_id == cell.id
    html = resp.get_data(as_text=True)
    assert box.box_number in html
    # Форма готова к следующему скану в ту же ячейку — без повторного ввода кода.
    assert 'value="B-02"' in html


def test_add_box_from_another_warehouse_is_rejected(db, client_logged_in):
    warehouse = _make_warehouse("6")
    other_warehouse = _make_warehouse("6B")
    item = _make_item("6")
    cell = Cell(warehouse_id=warehouse.id, code="B-03")
    db.session.add(cell)
    db.session.commit()
    box = _make_box(other_warehouse, item, "BOX-SCANCELL-6", qty=1)

    resp = client_logged_in.post(
        "/placement/scan-cell/add-box",
        data={"warehouse_id": warehouse.id, "cell_code": "B-03", "box_number": box.box_number},
        follow_redirects=True,
    )

    assert Box.query.get(box.id).cell_id is None
    assert "числится на складе" in resp.get_data(as_text=True)


def test_add_empty_box_is_rejected(db, client_logged_in):
    warehouse = _make_warehouse("7")
    cell = Cell(warehouse_id=warehouse.id, code="B-04")
    db.session.add(cell)
    db.session.commit()
    box = _make_box(warehouse, None, "BOX-SCANCELL-7")

    resp = client_logged_in.post(
        "/placement/scan-cell/add-box",
        data={"warehouse_id": warehouse.id, "cell_code": "B-04", "box_number": box.box_number},
        follow_redirects=True,
    )

    assert Box.query.get(box.id).cell_id is None
    assert "пуст" in resp.get_data(as_text=True)


def test_add_unknown_box_is_rejected(db, client_logged_in):
    warehouse = _make_warehouse("8")
    cell = Cell(warehouse_id=warehouse.id, code="B-05")
    db.session.add(cell)
    db.session.commit()

    resp = client_logged_in.post(
        "/placement/scan-cell/add-box",
        data={"warehouse_id": warehouse.id, "cell_code": "B-05", "box_number": "NOPE"},
        follow_redirects=True,
    )

    assert "не найден" in resp.get_data(as_text=True)


def test_scan_cell_code_matching_a_row_shows_row_screen(db, client_logged_in):
    """Помещения без возможности завести ячейки (см. чат) — если введенный
    код совпадает не с ячейкой, а с рядом, экран переключается на ряд."""
    warehouse = _make_warehouse("9")
    item = _make_item("9")
    zone = Zone(warehouse_id=warehouse.id, code="ROW-A")
    db.session.add(zone)
    db.session.commit()
    existing_box = _make_box(warehouse, item, "BOX-SCANCELL-9A", qty=1)
    existing_box.zone_id = zone.id
    existing_box.status = "stored"
    db.session.commit()

    html = client_logged_in.get(
        f"/placement/scan-cell?warehouse_id={warehouse.id}&cell_code=ROW-A"
    ).get_data(as_text=True)

    assert "Ряд ROW-A" in html
    assert "без ограничения по вместимости" in html
    assert existing_box.box_number in html
    assert 'name="box_number"' in html


def test_add_box_with_row_code_places_it_directly_in_the_row(db, client_logged_in):
    warehouse = _make_warehouse("10")
    item = _make_item("10")
    zone = Zone(warehouse_id=warehouse.id, code="ROW-B")
    db.session.add(zone)
    db.session.commit()
    box = _make_box(warehouse, item, "BOX-SCANCELL-10", qty=2)

    resp = client_logged_in.post(
        "/placement/scan-cell/add-box",
        data={"warehouse_id": warehouse.id, "cell_code": "ROW-B", "box_number": box.box_number},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    placed = Box.query.get(box.id)
    assert placed.cell_id is None
    assert placed.zone_id == zone.id
    assert placed.status == "stored"
    html = resp.get_data(as_text=True)
    assert "ряду ROW-B" in html
