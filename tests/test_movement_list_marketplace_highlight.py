"""Список перемещений подсвечивает строку по маркетплейсу склада
назначения (см. чат) — ВБ цветом #9F69D6 (класс mp-row-wb), Озон цветом
#594CD8 (класс mp-row-ozon), остальные (не маркетплейс, обычный
внутренний склад) — без подсветки."""

from datetime import datetime

from wms.extensions import db
from wms.models import MovementDocument, Warehouse


def _make_movement(number, marketplace=None, code_suffix=None):
    suffix = code_suffix or number
    sender = Warehouse(code=f"WH-MPH-{suffix}-A", name="Склад-отправитель")
    dest_kwargs = {"marketplace": marketplace} if marketplace else {}
    dest = Warehouse(
        code=f"WH-MPH-{suffix}-B",
        name=f"{(marketplace or 'обычный').upper()}: склад",
        **dest_kwargs,
    )
    db.session.add_all([sender, dest])
    db.session.commit()

    doc = MovementDocument(number=number, from_warehouse_id=sender.id, to_warehouse_id=dest.id, status="draft")
    db.session.add(doc)
    db.session.commit()
    return doc


def test_wb_destination_gets_wb_highlight_class(db, client_logged_in):
    doc = _make_movement("PER-MPH-1", marketplace="wb")
    html = client_logged_in.get("/movement/").get_data(as_text=True)
    row_start = html.index(doc.number)
    row_html = html[max(0, row_start - 400):row_start]
    assert "mp-row-wb" in row_html
    row_end = html.index("</tr>", row_start)
    assert ">ВБ</span>" in html[row_start:row_end]


def test_ozon_destination_gets_ozon_highlight_class(db, client_logged_in):
    doc = _make_movement("PER-MPH-2", marketplace="ozon")
    html = client_logged_in.get("/movement/").get_data(as_text=True)
    row_start = html.index(doc.number)
    row_html = html[max(0, row_start - 400):row_start]
    assert "mp-row-ozon" in row_html
    row_end = html.index("</tr>", row_start)
    assert ">ОЗОН</span>" in html[row_start:row_end]


def test_non_marketplace_destination_gets_no_highlight_class(db, client_logged_in):
    doc = _make_movement("PER-MPH-3")
    html = client_logged_in.get("/movement/").get_data(as_text=True)
    row_start = html.index(doc.number)
    row_html = html[max(0, row_start - 400):row_start]
    assert "mp-row-wb" not in row_html
    assert "mp-row-ozon" not in row_html


def test_movement_row_is_clickable_and_open_link_is_removed(db, client_logged_in):
    doc = _make_movement("PER-MPH-4", marketplace="ozon")
    html = client_logged_in.get("/movement/").get_data(as_text=True)

    assert f'data-href="/movement/{doc.id}"' in html
    assert "movement-clickable-row" in html
    assert "movement-number text-decoration-none" in html
    assert ">Открыть</a>" not in html


def test_receive_button_has_no_underline_class(db, client_logged_in):
    doc = _make_movement("PER-MPH-5", marketplace="wb")
    doc.status = "completed"
    doc.completed_at = datetime.utcnow()
    doc.marketplace_request_created_at = datetime.utcnow()
    db.session.commit()

    html = client_logged_in.get("/movement/").get_data(as_text=True)
    assert "movement-receive-btn text-decoration-none" in html
