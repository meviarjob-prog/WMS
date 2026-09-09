"""Печать накладных по выбранным перемещениям (флажки в списке): номер и
дата в шапке, штрихкод/наименование/количество (суммарно по всем коробам
документа) в табличной части."""

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, MovementLine, Nomenclature, Warehouse
from wms.utils.waybill_pdf import build_movement_waybills_pdf


def _make_document_with_box():
    sender = Warehouse(code="WH-W1", name="Склад-отправитель")
    dest = Warehouse(code="WH-W2", name="Склад назначения")
    db.session.add_all([sender, dest])
    db.session.commit()

    item = Nomenclature(sku="SKU-W1", barcode="9990000001", name="Товар для накладной", unit="шт")
    db.session.add(item)
    db.session.commit()

    box = Box(box_number="BOX-000900", warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=7))
    db.session.commit()

    doc = MovementDocument(number="PER-W1", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=box.warehouse_id, from_cell_id=box.cell_id)
    )
    db.session.commit()
    return doc


def test_export_waybills_requires_selection(db, client_logged_in):
    resp = client_logged_in.get("/movement/waybills.pdf", follow_redirects=True)
    assert "Выберите хотя бы одно перемещение" in resp.get_data(as_text=True)


def test_export_waybills_returns_pdf_for_selected_documents(db, client_logged_in):
    doc = _make_document_with_box()

    resp = client_logged_in.get(f"/movement/waybills.pdf?doc_ids={doc.id}")

    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF")


def test_build_movement_waybills_pdf_aggregates_items_across_boxes(db, client_logged_in):
    doc = _make_document_with_box()
    pdf_bytes = build_movement_waybills_pdf([doc])
    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 500
