"""Ручная отметка бухгалтера "внесено в 1С" в списке перемещений — просто
переключаемая галочка, независимая от автоматической выгрузки в 1С
(MovementDocument.synced_to_1c_at). Переключается через fetch() (см.
movement/list.html) — сама кнопка при этом несет оба варианта иконки в
data-атрибутах (для JS), поэтому проверяем именно ВИДИМЫЙ текст кнопки
(между > и </button>), а не просто наличие символа где-то в HTML."""

import re

from wms.models import MovementDocument, Warehouse
from wms.utils.timezone import to_moscow


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


def _accounting_button_text(html, doc_id):
    match = re.search(
        rf'data-url="/movement/{doc_id}/toggle-accounting"[^>]*>([^<]*)</button>', html
    )
    assert match, "Кнопка галочки '1С' не найдена в HTML"
    return match.group(1).strip()


def test_list_shows_unchecked_by_default(db, client_logged_in):
    doc = _make_document()

    html = client_logged_in.get("/movement/").get_data(as_text=True)

    assert _accounting_button_text(html, doc.id) == "☐"


def test_toggle_sets_and_clears_timestamp(db, client_logged_in):
    doc = _make_document()

    resp = client_logged_in.post(f"/movement/{doc.id}/toggle-accounting")
    assert doc.accounting_entered_at is not None
    assert resp.get_json() == {
        "ok": True,
        "checked": True,
        "at": to_moscow(doc.accounting_entered_at).strftime("%d.%m.%Y %H:%M"),
    }

    html = client_logged_in.get("/movement/").get_data(as_text=True)
    assert _accounting_button_text(html, doc.id) == "✅"

    resp = client_logged_in.post(f"/movement/{doc.id}/toggle-accounting")
    assert doc.accounting_entered_at is None
    assert resp.get_json() == {"ok": True, "checked": False, "at": None}
