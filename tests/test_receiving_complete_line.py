"""На разбраковке можно завершать приемку выборочно, по одной строке (см.
receiving.complete_line), не дожидаясь, пока проверят остальные строки —
как только завершена последняя открытая строка, документ целиком переходит
в completed, так же, как при обычном "Завершить приемку" (complete())."""

from wms.extensions import db
from wms.models import Nomenclature, ReceivingDocument, ReceivingLine, SupplierReturn, UnplacedStock, Warehouse


def _make_warehouse(suffix):
    wh = Warehouse(code=f"WH-CL{suffix}", name="Склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(suffix):
    item = Nomenclature(sku=f"SKU-CL{suffix}", barcode=f"77710000{suffix}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_sorting_doc(wh, number, items_qty):
    """items_qty — список (item, qty); документ уже на разбраковке, все
    строки подтверждены (как будто пересчет прошел без расхождений)."""
    doc = ReceivingDocument(number=number, warehouse_id=wh.id, invoice_file_name="накладная.xlsx", status="sorting")
    db.session.add(doc)
    db.session.commit()
    for item, qty in items_qty:
        db.session.add(
            ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=qty, expected_qty=qty, confirmed=True)
        )
    db.session.commit()
    return doc


def test_complete_line_credits_only_that_line_and_keeps_document_open(db, client_logged_in):
    wh = _make_warehouse("1")
    item1 = _make_item("1a")
    item2 = _make_item("1b")
    doc = _make_sorting_doc(wh, "CL-0001", [(item1, 5), (item2, 7)])
    line1 = ReceivingLine.query.filter_by(document_id=doc.id, nomenclature_id=item1.id).first()

    resp = client_logged_in.post(f"/receiving/{doc.id}/lines/{line1.id}/complete-line", follow_redirects=True)

    assert resp.status_code == 200
    assert UnplacedStock.available(wh.id, item1.id) == 5
    assert UnplacedStock.available(wh.id, item2.id) == 0
    doc = ReceivingDocument.query.get(doc.id)
    assert doc.status == "sorting"
    assert ReceivingLine.query.get(line1.id).line_completed_at is not None


def test_completing_last_remaining_line_finishes_whole_document(db, client_logged_in):
    wh = _make_warehouse("2")
    item1 = _make_item("2a")
    item2 = _make_item("2b")
    doc = _make_sorting_doc(wh, "CL-0002", [(item1, 3), (item2, 4)])
    line1 = ReceivingLine.query.filter_by(document_id=doc.id, nomenclature_id=item1.id).first()
    line2 = ReceivingLine.query.filter_by(document_id=doc.id, nomenclature_id=item2.id).first()

    client_logged_in.post(f"/receiving/{doc.id}/lines/{line1.id}/complete-line")
    resp = client_logged_in.post(f"/receiving/{doc.id}/lines/{line2.id}/complete-line", follow_redirects=True)

    assert resp.status_code == 200
    doc = ReceivingDocument.query.get(doc.id)
    assert doc.status == "completed"
    assert doc.completed_at is not None
    assert UnplacedStock.available(wh.id, item1.id) == 3
    assert UnplacedStock.available(wh.id, item2.id) == 4


def test_complete_line_creates_return_for_defect(db, client_logged_in):
    wh = _make_warehouse("3")
    item = _make_item("3")
    doc = _make_sorting_doc(wh, "CL-0003", [(item, 10)])
    line = ReceivingLine.query.filter_by(document_id=doc.id).first()
    client_logged_in.post(f"/receiving/{doc.id}/lines/{line.id}/update-defect", data={"defect_qty": "4"})

    client_logged_in.post(f"/receiving/{doc.id}/lines/{line.id}/complete-line")

    assert UnplacedStock.available(wh.id, item.id) == 6
    ret = SupplierReturn.query.filter_by(receiving_document_id=doc.id).first()
    assert ret is not None
    assert ret.qty == 4


def test_whole_document_complete_skips_already_completed_lines(db, client_logged_in):
    """"Завершить приемку" целиком не должно зачислить уже завершенную по
    отдельности строку повторно."""
    wh = _make_warehouse("4")
    item1 = _make_item("4a")
    item2 = _make_item("4b")
    doc = _make_sorting_doc(wh, "CL-0004", [(item1, 5), (item2, 6)])
    line1 = ReceivingLine.query.filter_by(document_id=doc.id, nomenclature_id=item1.id).first()

    client_logged_in.post(f"/receiving/{doc.id}/lines/{line1.id}/complete-line")
    resp = client_logged_in.post(f"/receiving/{doc.id}/complete", follow_redirects=True)

    assert resp.status_code == 200
    doc = ReceivingDocument.query.get(doc.id)
    assert doc.status == "completed"
    assert UnplacedStock.available(wh.id, item1.id) == 5
    assert UnplacedStock.available(wh.id, item2.id) == 6


def test_cannot_complete_line_twice(db, client_logged_in):
    """Второй товар в документе держит его на разбраковке — иначе после
    завершения единственной строки документ сразу перейдет в completed, и
    вторая попытка упрется в проверку статуса раньше, чем в "уже завершена"."""
    wh = _make_warehouse("5")
    item = _make_item("5")
    other_item = _make_item("5b")
    doc = _make_sorting_doc(wh, "CL-0005", [(item, 3), (other_item, 1)])
    line = ReceivingLine.query.filter_by(document_id=doc.id, nomenclature_id=item.id).first()
    client_logged_in.post(f"/receiving/{doc.id}/lines/{line.id}/complete-line")

    resp = client_logged_in.post(f"/receiving/{doc.id}/lines/{line.id}/complete-line", follow_redirects=True)

    assert "уже завершена" in resp.get_data(as_text=True)
    assert UnplacedStock.available(wh.id, item.id) == 3


def test_update_defect_blocked_once_line_completed(db, client_logged_in):
    wh = _make_warehouse("6")
    item = _make_item("6")
    other_item = _make_item("6b")
    doc = _make_sorting_doc(wh, "CL-0006", [(item, 5), (other_item, 1)])
    line = ReceivingLine.query.filter_by(document_id=doc.id, nomenclature_id=item.id).first()
    client_logged_in.post(f"/receiving/{doc.id}/lines/{line.id}/complete-line")

    resp = client_logged_in.post(
        f"/receiving/{doc.id}/lines/{line.id}/update-defect", data={"defect_qty": "1"}, follow_redirects=True
    )

    assert "уже завершена" in resp.get_data(as_text=True)
    assert ReceivingLine.query.get(line.id).defect_qty == 0


def test_complete_line_requires_invoice_import_or_admin(db, client):
    from wms.models import User

    wh = _make_warehouse("7")
    item = _make_item("7")
    doc = _make_sorting_doc(wh, "CL-0007", [(item, 2)])
    doc.invoice_file_name = None
    db.session.commit()
    line = ReceivingLine.query.filter_by(document_id=doc.id).first()

    user = User(username="staffer-cl", full_name="Складской", role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True

    client.post(f"/receiving/{doc.id}/lines/{line.id}/complete-line")

    assert ReceivingLine.query.get(line.id).line_completed_at is None


def test_revert_to_sorting_resets_line_completed_at(db, client_logged_in):
    wh = _make_warehouse("8")
    item = _make_item("8")
    doc = _make_sorting_doc(wh, "CL-0008", [(item, 5)])
    line = ReceivingLine.query.filter_by(document_id=doc.id).first()
    client_logged_in.post(f"/receiving/{doc.id}/lines/{line.id}/complete-line")
    assert ReceivingDocument.query.get(doc.id).status == "completed"

    client_logged_in.post(f"/receiving/{doc.id}/revert-to-sorting")

    doc = ReceivingDocument.query.get(doc.id)
    assert doc.status == "sorting"
    assert ReceivingLine.query.get(line.id).line_completed_at is None
    assert UnplacedStock.available(wh.id, item.id) == 0
