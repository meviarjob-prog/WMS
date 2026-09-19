"""Галочка "Проверено в 1С" в списке приемок (см. чат) — чисто ручная
отметка для контроля бухгалтером, не влияет на сам документ и не связана с
выгрузкой в 1С (в отличие от ReceivingDocument.accounting_entered_at,
который гейтит очередь корректировок пересчета). Доступна только для
приемок, загруженных из накладной (is_from_invoice_import)."""

from wms.extensions import db
from wms.models import ReceivingDocument, Warehouse


def _make_warehouse(code="WH-CHK1C"):
    wh = Warehouse(code=code, name="Тест склад проверки в 1С")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_invoice_doc(wh, number):
    doc = ReceivingDocument(number=number, warehouse_id=wh.id, invoice_file_name="накладная.xlsx")
    db.session.add(doc)
    db.session.commit()
    return doc


def _make_manual_doc(wh, number):
    doc = ReceivingDocument(number=number, warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()
    return doc


def test_list_shows_unchecked_checkbox_for_invoice_document(db, client_logged_in):
    wh = _make_warehouse("1")
    doc = _make_invoice_doc(wh, "CHK-0001")

    html = client_logged_in.get("/receiving/").get_data(as_text=True)
    idx = html.index(doc.number)
    row_html = html[idx : idx + 3000]
    assert "toggle-flag-btn" in row_html
    assert "toggle-checked-in-1c" in row_html
    assert "☐" in row_html


def test_toggle_checked_in_1c_sets_and_clears_timestamp(db, client_logged_in):
    wh = _make_warehouse("2")
    doc = _make_invoice_doc(wh, "CHK-0002")

    resp = client_logged_in.post(f"/receiving/{doc.id}/toggle-checked-in-1c")
    data = resp.get_json()
    assert data["ok"] is True
    assert data["checked"] is True
    assert data["at"] is not None
    doc = ReceivingDocument.query.get(doc.id)
    assert doc.checked_in_1c_at is not None

    resp = client_logged_in.post(f"/receiving/{doc.id}/toggle-checked-in-1c")
    data = resp.get_json()
    assert data["checked"] is False
    assert data["at"] is None
    doc = ReceivingDocument.query.get(doc.id)
    assert doc.checked_in_1c_at is None


def test_toggle_does_not_affect_other_fields(db, client_logged_in):
    wh = _make_warehouse("3")
    doc = _make_invoice_doc(wh, "CHK-0003")

    client_logged_in.post(f"/receiving/{doc.id}/toggle-checked-in-1c")

    doc = ReceivingDocument.query.get(doc.id)
    assert doc.accounting_entered_at is None
    assert doc.recount_synced_to_1c_at is None
    assert doc.status == "draft"


def test_list_hides_checkbox_for_manual_receiving(db, client_logged_in):
    wh = _make_warehouse("4")
    doc = _make_manual_doc(wh, "CHK-0004")

    html = client_logged_in.get("/receiving/").get_data(as_text=True)
    idx = html.index(doc.number)
    row_html = html[idx : idx + 1500]
    assert "toggle-flag-btn" not in row_html
    assert "toggle-checked-in-1c" not in row_html


def test_checked_document_shows_checkmark_in_list(db, client_logged_in):
    wh = _make_warehouse("5")
    doc = _make_invoice_doc(wh, "CHK-0005")
    client_logged_in.post(f"/receiving/{doc.id}/toggle-checked-in-1c")

    html = client_logged_in.get("/receiving/").get_data(as_text=True)
    idx = html.index(doc.number)
    row_html = html[idx : idx + 3000]
    assert "✅" in row_html
