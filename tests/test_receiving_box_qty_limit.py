"""Жесткий запрет: в коробе не может быть больше 300 шт суммарно (см. чат:
"также добавь запрет на ввод кол-ва в коробе больше 300 шт", "при приемке").

В отличие от test_receiving_box_qty_warning.py (мягкое предупреждение по
ОДНОМУ виду товара, не блокирует) — это жесткий запрет по ВСЕМУ коробу
целиком, независимо от вида."""

from wms.blueprints.receiving import BOX_QTY_LIMIT
from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, ReceivingDocument, Warehouse


def _make_doc_with_box(client_logged_in, warehouse):
    resp = client_logged_in.post(
        "/receiving/new", data={"warehouse_id": warehouse.id, "supplier": ""}, follow_redirects=True
    )
    doc_id = int(resp.request.path.rstrip("/").rsplit("/", 1)[-1])
    client_logged_in.post(f"/receiving/{doc_id}/boxes/create")
    box = Box.query.filter_by(warehouse_id=warehouse.id).order_by(Box.id.desc()).first()
    return doc_id, box.id


def test_scanning_over_limit_is_blocked_with_json_error(db, client_logged_in):
    warehouse = Warehouse(code="WH-BL1", name="Склад для проверки лимита короба")
    db.session.add(warehouse)
    db.session.commit()
    item = Nomenclature(sku="SKU-BL1", barcode="7771000001", name="Товар лимит-тест", unit="шт")
    db.session.add(item)
    db.session.commit()

    doc_id, box_id = _make_doc_with_box(client_logged_in, warehouse)

    resp_ok = client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box_id}/lines/add-by-barcode",
        json={"barcode": item.barcode, "qty": 300},
    )
    assert resp_ok.get_json()["ok"] is True

    resp_over = client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box_id}/lines/add-by-barcode",
        json={"barcode": item.barcode, "qty": 1},
    )
    data = resp_over.get_json()
    assert resp_over.status_code == 400
    assert data["ok"] is False
    assert "300" in data["error"]

    box_item = BoxItem.query.filter_by(box_id=box_id, nomenclature_id=item.id).first()
    assert box_item.qty == 300  # вторая вставка не прошла


def test_manual_add_over_limit_flashes_error_and_does_not_add(db, client_logged_in):
    warehouse = Warehouse(code="WH-BL2", name="Склад для проверки лимита короба 2")
    db.session.add(warehouse)
    db.session.commit()
    item = Nomenclature(sku="SKU-BL2", barcode="7771000002", name="Товар лимит-тест 2", unit="шт")
    db.session.add(item)
    db.session.commit()

    doc_id, box_id = _make_doc_with_box(client_logged_in, warehouse)

    resp = client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box_id}/lines/add",
        data={"nomenclature_id": item.id, "qty": 301},
        follow_redirects=True,
    )

    html = resp.get_data(as_text=True)
    assert "максимум 300" in html
    box_item = BoxItem.query.filter_by(box_id=box_id, nomenclature_id=item.id).first()
    assert box_item is None


def test_add_exactly_limit_is_allowed(db, client_logged_in):
    warehouse = Warehouse(code="WH-BL3", name="Склад для проверки лимита короба 3")
    db.session.add(warehouse)
    db.session.commit()
    item = Nomenclature(sku="SKU-BL3", barcode="7771000003", name="Товар лимит-тест 3", unit="шт")
    db.session.add(item)
    db.session.commit()

    doc_id, box_id = _make_doc_with_box(client_logged_in, warehouse)

    resp = client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box_id}/lines/add-by-barcode",
        json={"barcode": item.barcode, "qty": BOX_QTY_LIMIT},
    )
    data = resp.get_json()
    assert data["ok"] is True
    box_item = BoxItem.query.filter_by(box_id=box_id, nomenclature_id=item.id).first()
    assert box_item.qty == BOX_QTY_LIMIT


def test_update_line_over_limit_is_blocked(db, client_logged_in):
    warehouse = Warehouse(code="WH-BL4", name="Склад для проверки лимита короба 4")
    db.session.add(warehouse)
    db.session.commit()
    item = Nomenclature(sku="SKU-BL4", barcode="7771000004", name="Товар лимит-тест 4", unit="шт")
    db.session.add(item)
    db.session.commit()

    doc_id, box_id = _make_doc_with_box(client_logged_in, warehouse)
    client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box_id}/lines/add-by-barcode",
        json={"barcode": item.barcode, "qty": 100},
    )
    doc = ReceivingDocument.query.get(doc_id)
    line = doc.lines.first()

    resp = client_logged_in.post(
        f"/receiving/{doc_id}/lines/{line.id}/update",
        data={"qty": 301},
        follow_redirects=True,
    )
    html = resp.get_data(as_text=True)
    assert "максимум 300" in html

    box_item = BoxItem.query.filter_by(box_id=box_id, nomenclature_id=item.id).first()
    assert box_item.qty == 100  # не изменилось


def test_confirm_line_over_limit_returns_json_error(db, client_logged_in):
    warehouse = Warehouse(code="WH-BL5", name="Склад для проверки лимита короба 5")
    db.session.add(warehouse)
    db.session.commit()
    item = Nomenclature(sku="SKU-BL5", barcode="7771000005", name="Товар лимит-тест 5", unit="шт")
    db.session.add(item)
    db.session.commit()

    doc_id, box_id = _make_doc_with_box(client_logged_in, warehouse)
    client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box_id}/lines/add-by-barcode",
        json={"barcode": item.barcode, "qty": 100},
    )
    doc = ReceivingDocument.query.get(doc_id)
    line = doc.lines.first()

    resp = client_logged_in.post(
        f"/receiving/{doc_id}/lines/{line.id}/confirm",
        json={"qty": 400, "confirmed": True},
    )
    data = resp.get_json()
    assert resp.status_code == 400
    assert data["ok"] is False
    assert "300" in data["error"]


def test_bulk_update_skips_line_over_limit_but_applies_others(db, client_logged_in):
    warehouse = Warehouse(code="WH-BL6", name="Склад для проверки лимита короба 6")
    db.session.add(warehouse)
    db.session.commit()
    item_a = Nomenclature(sku="SKU-BL6A", barcode="7771000006", name="Товар лимит-тест 6А", unit="шт")
    item_b = Nomenclature(sku="SKU-BL6B", barcode="7771000007", name="Товар лимит-тест 6Б", unit="шт")
    db.session.add_all([item_a, item_b])
    db.session.commit()

    doc_id, box_id = _make_doc_with_box(client_logged_in, warehouse)
    client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box_id}/lines/add-by-barcode",
        json={"barcode": item_a.barcode, "qty": 100},
    )
    client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box_id}/lines/add-by-barcode",
        json={"barcode": item_b.barcode, "qty": 50},
    )
    doc = ReceivingDocument.query.get(doc_id)
    line_a = doc.lines.filter_by(nomenclature_id=item_a.id).first()
    line_b = doc.lines.filter_by(nomenclature_id=item_b.id).first()

    resp = client_logged_in.post(
        f"/receiving/{doc_id}/lines/update-bulk",
        data={f"qty_{line_a.id}": "280", f"qty_{line_b.id}": "60"},
        follow_redirects=True,
    )
    html = resp.get_data(as_text=True)
    assert "не обновлено" in html

    box_item_a = BoxItem.query.filter_by(box_id=box_id, nomenclature_id=item_a.id).first()
    box_item_b = BoxItem.query.filter_by(box_id=box_id, nomenclature_id=item_b.id).first()
    assert box_item_a.qty == 100  # 280+50=330 > 300 — эта строка пропущена
    assert box_item_b.qty == 60  # эта строка обновилась нормально


def test_limit_is_per_box_not_per_document(db, client_logged_in):
    """300 — лимит на КОРОБ, а не на документ приемки: второй короб того же
    документа не унаследует "занятость" первого."""
    warehouse = Warehouse(code="WH-BL7", name="Склад для проверки лимита короба 7")
    db.session.add(warehouse)
    db.session.commit()
    item = Nomenclature(sku="SKU-BL7", barcode="7771000008", name="Товар лимит-тест 7", unit="шт")
    db.session.add(item)
    db.session.commit()

    doc_id, box1_id = _make_doc_with_box(client_logged_in, warehouse)
    client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box1_id}/lines/add-by-barcode",
        json={"barcode": item.barcode, "qty": 300},
    )
    client_logged_in.post(f"/receiving/{doc_id}/boxes/create")
    box2 = Box.query.filter_by(warehouse_id=warehouse.id).order_by(Box.id.desc()).first()

    resp = client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box2.id}/lines/add-by-barcode",
        json={"barcode": item.barcode, "qty": 300},
    )
    assert resp.get_json()["ok"] is True
