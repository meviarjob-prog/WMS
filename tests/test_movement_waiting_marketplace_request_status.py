"""После завершения сборки перемещение должно показывать статус «Ждет заявки
на МП», а не «В пути» — в том числе пользователю, который не является
автором документа и не админ. «В пути» появляется только после отметки
«Транспорт забрал» (shipped_at)."""

from datetime import datetime

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, MovementLine, Nomenclature, User, Warehouse


def _make_completed_doc(suffix):
    sender = Warehouse(code=f"WH-WMR{suffix}A", name="Склад-отправитель")
    dest = Warehouse(code=f"WH-WMR{suffix}B", name="ОЗОН: склад", marketplace="ozon")
    db.session.add_all([sender, dest])
    db.session.commit()
    item = Nomenclature(sku=f"SKU-WMR{suffix}", barcode=f"7771200{suffix}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    box = Box(box_number=f"BOX-WMR{suffix}", warehouse_id=dest.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=1))
    doc = MovementDocument(
        number=f"PER-WMR{suffix}", from_warehouse_id=sender.id, to_warehouse_id=dest.id, status="completed"
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
    db.session.commit()
    return doc


def _login_as_other_user(client):
    user = User(username="wmr-viewer", is_admin=False, role="warehouse", movement_view_allowed=True)
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_transit_status_label_follows_stages(db):
    doc = _make_completed_doc("1")
    assert doc.transit_status_label() == "Ждет заявки на МП"

    doc.marketplace_request_number = "REQ-1"
    doc.marketplace_request_created_at = datetime.utcnow()
    assert doc.transit_status_label() == "Ожидает транспорт"

    doc.shipped_at = datetime.utcnow()
    assert doc.transit_status_label() == "В пути"


def test_completed_movement_waits_for_marketplace_request_for_non_author(db, client):
    doc = _make_completed_doc("2")
    _login_as_other_user(client)

    detail = client.get(f"/movement/{doc.id}").get_data(as_text=True)
    listing = client.get("/movement/").get_data(as_text=True)

    for html in (detail, listing):
        assert "Ждет заявки на МП" in html
        assert "В пути</span>" not in html


def test_admin_sees_same_status_as_other_users(db, client_logged_in):
    waiting = _make_completed_doc("3")
    shipped = _make_completed_doc("4")
    shipped.marketplace_request_number = "REQ-4"
    shipped.marketplace_request_created_at = datetime.utcnow()
    shipped.shipped_at = datetime.utcnow()
    db.session.commit()

    waiting_html = client_logged_in.get(f"/movement/{waiting.id}").get_data(as_text=True)
    shipped_html = client_logged_in.get(f"/movement/{shipped.id}").get_data(as_text=True)

    assert "Ждет заявки на МП</span>" in waiting_html
    assert "Сохранить номер" in waiting_html
    assert "В пути</span>" in shipped_html
    assert "Принято на складе МП" in shipped_html
