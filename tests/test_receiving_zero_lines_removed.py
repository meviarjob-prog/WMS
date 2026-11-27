"""Приемка по накладной: товар, которого совсем не привезли (на пересчете
выставлен ноль), удаляется из документа при переходе на разбраковку —
чтобы при сверке расхождений не было строк-нулей. Запись об удаленном
остается (removed_zero_lines): видна на странице и уходит в 1С нулем."""

from wms.extensions import db
from wms.models import Nomenclature, ReceivingDocument, ReceivingLine, Warehouse
from wms.blueprints.integration_1c import _receiving_adjustments_candidates, _receiving_adjustments_export


def _doc():
    wh = Warehouse(code="WH-ZL", name="Склад ZL")
    db.session.add(wh)
    db.session.commit()
    items = []
    for i in range(3):
        item = Nomenclature(sku=f"ZL-{i}", barcode=f"ZL000{i}", name=f"Позиция {i}", unit="шт")
        db.session.add(item)
        items.append(item)
    db.session.commit()
    doc = ReceivingDocument(number="ZL-0001", warehouse_id=wh.id, supplier="ИП", invoice_file_name="inv.xlsx",
                            status="recounting")
    db.session.add(doc)
    db.session.commit()
    lines = [
        ReceivingLine(document_id=doc.id, nomenclature_id=items[0].id, qty=10, expected_qty=10, confirmed=True),
        ReceivingLine(document_id=doc.id, nomenclature_id=items[1].id, qty=0, expected_qty=40, confirmed=True),
        ReceivingLine(document_id=doc.id, nomenclature_id=items[2].id, qty=7, expected_qty=9, confirmed=True),
    ]
    db.session.add_all(lines)
    db.session.commit()
    return doc, items


def test_zero_lines_removed_on_send_to_sorting_but_recorded(db, client_logged_in):
    doc, items = _doc()
    resp = client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting", follow_redirects=True)
    html = resp.get_data(as_text=True)

    doc = ReceivingDocument.query.get(doc.id)
    assert doc.status == "sorting"
    assert sorted(l.qty for l in doc.lines) == [7, 10]            # нулевая строка удалена
    assert doc.removed_zero_items() == [{"barcode": "ZL0001", "name": "Позиция 1", "expected_qty": 40}]
    assert "Удалены строки с нулевым количеством" in html
    assert "Не привезли" in html and "Позиция 1" in html


def test_nothing_removed_when_no_zero_lines(db, client_logged_in):
    doc, items = _doc()
    ReceivingLine.query.filter_by(document_id=doc.id, qty=0).first().qty = 3
    db.session.commit()
    client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting")
    doc = ReceivingDocument.query.get(doc.id)
    assert doc.lines.count() == 3 and doc.removed_zero_lines is None


def test_zero_lines_are_not_sent_to_1c_or_reconciliation(db, client_logged_in):
    from wms.blueprints.integration_1c import _receiving_reconciliation_payload

    doc, items = _doc()
    client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting")
    payload = _receiving_adjustments_export()[0]
    assert {l["barcode"]: l["qty"] for l in payload["lines"]} == {"ZL0000": 10, "ZL0002": 7}
    doc = ReceivingDocument.query.get(doc.id)
    assert {l["barcode"] for l in _receiving_reconciliation_payload(doc)["lines"]} == {"ZL0000", "ZL0002"}


def test_legacy_zero_line_is_not_a_1c_discrepancy_even_if_still_in_wms(db):
    from wms.blueprints.integration_1c import _receiving_reconciliation_payload

    doc, items = _doc()
    doc.status = "sorting"
    ReceivingLine.query.filter_by(document_id=doc.id, nomenclature_id=items[2].id).first().qty = 9
    db.session.commit()
    # единственное расхождение — нулевая строка: в 1С ничего отправлять не нужно
    assert _receiving_adjustments_candidates() == []
    assert "ZL0001" not in {l["barcode"] for l in _receiving_reconciliation_payload(doc)["lines"]}


def test_startup_cleanup_removes_old_zero_lines_once(db):
    from wms import _cleanup_zero_receiving_lines

    doc, items = _doc()
    doc.status = "completed"
    db.session.commit()
    _cleanup_zero_receiving_lines()
    doc = ReceivingDocument.query.get(doc.id)
    assert sorted(l.qty for l in doc.lines) == [7, 10]
    assert [i["barcode"] for i in doc.removed_zero_items()] == ["ZL0001"]
    _cleanup_zero_receiving_lines()   # повтор ничего не меняет
    assert len(ReceivingDocument.query.get(doc.id).removed_zero_items()) == 1


def test_draft_and_recounting_documents_keep_zero_lines_until_sorting(db):
    from wms import _cleanup_zero_receiving_lines

    doc, items = _doc()   # status = recounting
    _cleanup_zero_receiving_lines()
    assert ReceivingDocument.query.get(doc.id).lines.count() == 3
