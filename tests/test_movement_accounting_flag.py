"""Ручная отметка бухгалтера "внесено в 1С" в списке перемещений — просто
переключаемая галочка, независимая от автоматической выгрузки в 1С
(MovementDocument.synced_to_1c_at)."""

from wms.models import MovementDocument, Warehouse


def _make_document():
    wh1 = Warehouse(code="WH-A1", name="Склад-отправитель")
    wh2 = Warehouse(code="WH-A2", name="Склад назначения")
    from wms.extensions import db

    db.session.add_all([wh1, wh2])
    db.session.commit()
    doc = MovementDocument(number="PER-000001", from_warehouse_id=wh1.id, to_warehouse_id=wh2.id)
    db.session.add(doc)
    db.session.commit()
    return doc


def test_list_shows_unchecked_by_default(db, client_logged_in):
    doc = _make_document()

    resp = client_logged_in.get("/movement/")
    html = resp.get_data(as_text=True)
    idx = html.find(doc.number)

    assert "☐" in html[idx : idx + 800]
    assert "✅" not in html[idx : idx + 800]


def test_toggle_sets_and_clears_timestamp(db, client_logged_in):
    doc = _make_document()

    client_logged_in.post(f"/movement/{doc.id}/toggle-accounting")
    assert doc.accounting_entered_at is not None

    resp = client_logged_in.get("/movement/")
    html = resp.get_data(as_text=True)
    idx = html.find(doc.number)
    assert "✅" in html[idx : idx + 800]

    client_logged_in.post(f"/movement/{doc.id}/toggle-accounting")
    assert doc.accounting_entered_at is None
