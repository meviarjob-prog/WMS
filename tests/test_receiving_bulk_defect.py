"""Брак на разбраковке заполняется по всем строкам сразу и сохраняется
одним нажатием — либо общей кнопки "Завершить приемку" (complete()), либо
построчной "Готово" (complete_line()) — без отдельной перезагрузки
страницы на каждое изменение брака, как было раньше через update_defect
(см. receiving/detail.html и receiving/confirm_invoice.html: поле
defect_qty_<line_id> общее, без своей формы)."""

from wms.extensions import db
from wms.models import Nomenclature, ReceivingDocument, ReceivingLine, SupplierReturn, UnplacedStock, Warehouse


def _make_warehouse(suffix):
    wh = Warehouse(code=f"WH-BD{suffix}", name="Склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(suffix):
    item = Nomenclature(sku=f"SKU-BD{suffix}", barcode=f"77720000{suffix}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_sorting_doc(wh, number, items_qty, confirmed=True):
    doc = ReceivingDocument(number=number, warehouse_id=wh.id, invoice_file_name="накладная.xlsx", status="sorting")
    db.session.add(doc)
    db.session.commit()
    for item, qty in items_qty:
        db.session.add(
            ReceivingLine(
                document_id=doc.id, nomenclature_id=item.id, qty=qty, expected_qty=qty, confirmed=confirmed
            )
        )
    db.session.commit()
    return doc


def test_complete_applies_defect_qty_for_all_open_lines_at_once(db, client_logged_in):
    wh = _make_warehouse("1")
    item1 = _make_item("1a")
    item2 = _make_item("1b")
    doc = _make_sorting_doc(wh, "BD-0001", [(item1, 10), (item2, 5)])
    line1 = ReceivingLine.query.filter_by(document_id=doc.id, nomenclature_id=item1.id).first()
    line2 = ReceivingLine.query.filter_by(document_id=doc.id, nomenclature_id=item2.id).first()

    resp = client_logged_in.post(
        f"/receiving/{doc.id}/complete",
        data={f"defect_qty_{line1.id}": "3", f"defect_qty_{line2.id}": "0"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    doc = ReceivingDocument.query.get(doc.id)
    assert doc.status == "completed"
    assert UnplacedStock.available(wh.id, item1.id) == 7
    assert UnplacedStock.available(wh.id, item2.id) == 5
    ret = SupplierReturn.query.filter_by(receiving_document_id=doc.id).first()
    assert ret is not None
    assert ret.qty == 3
    assert ret.nomenclature_id == item1.id


def test_complete_rejects_defect_qty_exceeding_accepted_qty(db, client_logged_in):
    wh = _make_warehouse("2")
    item = _make_item("2")
    doc = _make_sorting_doc(wh, "BD-0002", [(item, 5)])
    line = ReceivingLine.query.filter_by(document_id=doc.id).first()

    resp = client_logged_in.post(
        f"/receiving/{doc.id}/complete",
        data={f"defect_qty_{line.id}": "999"},
        follow_redirects=True,
    )

    assert "не может быть отрицательным или больше принятого" in resp.get_data(as_text=True)
    doc = ReceivingDocument.query.get(doc.id)
    assert doc.status == "sorting"
    assert UnplacedStock.available(wh.id, item.id) == 0
    assert SupplierReturn.query.filter_by(receiving_document_id=doc.id).count() == 0


def test_complete_without_defect_fields_still_works(db, client_logged_in):
    """Обратная совместимость — запрос вообще без полей defect_qty_* (как
    делают старые тесты/клиенты) не должен ничего ломать."""
    wh = _make_warehouse("3")
    item = _make_item("3")
    doc = _make_sorting_doc(wh, "BD-0003", [(item, 4)])

    resp = client_logged_in.post(f"/receiving/{doc.id}/complete", follow_redirects=True)

    assert resp.status_code == 200
    doc = ReceivingDocument.query.get(doc.id)
    assert doc.status == "completed"
    assert UnplacedStock.available(wh.id, item.id) == 4


def test_complete_line_applies_defect_qty_from_shared_field(db, client_logged_in):
    wh = _make_warehouse("4")
    item1 = _make_item("4a")
    item2 = _make_item("4b")
    doc = _make_sorting_doc(wh, "BD-0004", [(item1, 8), (item2, 2)])
    line1 = ReceivingLine.query.filter_by(document_id=doc.id, nomenclature_id=item1.id).first()

    resp = client_logged_in.post(
        f"/receiving/{doc.id}/lines/{line1.id}/complete-line",
        data={f"defect_qty_{line1.id}": "2"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert UnplacedStock.available(wh.id, item1.id) == 6
    ret = SupplierReturn.query.filter_by(receiving_document_id=doc.id).first()
    assert ret is not None
    assert ret.qty == 2
    doc = ReceivingDocument.query.get(doc.id)
    assert doc.status == "sorting"  # item2 еще не завершен


def test_defect_entry_confirms_previously_unconfirmed_line_on_complete(db, client_logged_in):
    """Внесение брака прямо в форму завершения — такое же явное
    подтверждение принятого количества, как и раньше через update_defect
    (см. _apply_defect_qty_from_form) — даже если строку не подтверждали
    отдельно на пересчете."""
    wh = _make_warehouse("5")
    item = _make_item("5")
    doc = _make_sorting_doc(wh, "BD-0005", [(item, 6)], confirmed=False)
    line = ReceivingLine.query.filter_by(document_id=doc.id).first()

    client_logged_in.post(
        f"/receiving/{doc.id}/complete",
        data={f"defect_qty_{line.id}": "1"},
        follow_redirects=True,
    )

    doc = ReceivingDocument.query.get(doc.id)
    assert doc.status == "completed"
    assert UnplacedStock.available(wh.id, item.id) == 5
    assert ReceivingLine.query.get(line.id).confirmed is True


def test_unconfirmed_line_untouched_in_form_is_still_skipped(db, client_logged_in):
    """Строка, для которой поле defect_qty_<id> вообще не отправлено (ее не
    трогали на разбраковке), остается неподтвержденной и не зачисляется —
    в отличие от строки, где явно указали брак (пусть и 0), это НЕ
    равнозначное действие: только явный ввод считается подтверждением."""
    wh = _make_warehouse("6")
    item1 = _make_item("6a")
    item2 = _make_item("6b")
    doc = _make_sorting_doc(wh, "BD-0006", [(item1, 3), (item2, 4)], confirmed=False)
    line1 = ReceivingLine.query.filter_by(document_id=doc.id, nomenclature_id=item1.id).first()

    resp = client_logged_in.post(
        f"/receiving/{doc.id}/complete",
        data={f"defect_qty_{line1.id}": "0"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert UnplacedStock.available(wh.id, item1.id) == 3
    assert UnplacedStock.available(wh.id, item2.id) == 0
