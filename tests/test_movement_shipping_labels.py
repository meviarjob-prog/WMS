"""Стикеры отправления 58x40мм по выбранным перемещениям — один стикер на
каждый короб документа, с получателем склада назначения (настраивается в
«Склады и ячейки», см. warehouses.update_recipient) и отправителем."""

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, MovementLine, Nomenclature, Warehouse
from wms.utils.shipping_label_pdf import build_movement_shipping_labels_pdf


def _make_document_with_boxes(n_boxes=2):
    sender = Warehouse(code="WH-L1", name="Склад-отправитель")
    dest = Warehouse(code="WH-L2", name="ОЗОН: Казань", recipient_info="ООО Ромашка, ул. Тестовая 1, +7 900 000-00-00")
    db.session.add_all([sender, dest])
    db.session.commit()

    item = Nomenclature(sku="SKU-L1", barcode="7770000101", name="Товар для стикера", unit="шт")
    db.session.add(item)
    db.session.commit()

    doc = MovementDocument(number="PER-L1", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()

    for i in range(n_boxes):
        box = Box(box_number=f"BOX-00090{i}", warehouse_id=sender.id, status="open")
        db.session.add(box)
        db.session.commit()
        db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=3))
        db.session.add(
            MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=box.warehouse_id, from_cell_id=box.cell_id)
        )
        db.session.commit()

    return doc, dest


def test_export_shipping_labels_requires_selection(db, client_logged_in):
    resp = client_logged_in.get("/movement/shipping-labels.pdf", follow_redirects=True)
    assert "Выберите хотя бы одно перемещение" in resp.get_data(as_text=True)


def test_export_shipping_labels_returns_pdf(db, client_logged_in):
    doc, _dest = _make_document_with_boxes(n_boxes=2)

    resp = client_logged_in.get(f"/movement/shipping-labels.pdf?doc_ids={doc.id}")

    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF")


def test_build_shipping_labels_one_per_box(db, client_logged_in):
    doc, _dest = _make_document_with_boxes(n_boxes=3)

    pdf_bytes = build_movement_shipping_labels_pdf([doc])

    # Три отдельных страницы (по короб на стикер) — считаем маркеры начала
    # страницы PDF, это надежнее, чем парсить содержимое.
    assert pdf_bytes.count(b"/Type /Page") + pdf_bytes.count(b"/Type/Page") >= 3


def test_warehouse_recipient_update(db, client_logged_in):
    wh = Warehouse(code="WH-L3", name="ОЗОН: Тверь")
    db.session.add(wh)
    db.session.commit()

    resp = client_logged_in.post(
        f"/warehouses/{wh.id}/recipient",
        data={"recipient_info": "ИП Иванов, г. Тверь, ул. Ленина 5"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    wh = Warehouse.query.get(wh.id)
    assert wh.recipient_info == "ИП Иванов, г. Тверь, ул. Ленина 5"
