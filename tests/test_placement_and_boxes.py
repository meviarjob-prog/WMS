"""Размещение: пустые короба не должны засорять список "неразмещенных".
Короба: редактировать состав короба напрямую может только администратор."""

from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, PlacementDocument, User, Warehouse


def _make_warehouse():
    wh = Warehouse(code="WH-T", name="Тестовый склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def test_empty_box_hidden_from_other_open_boxes(db, client_logged_in):
    wh = _make_warehouse()
    empty_box = Box(box_number="BOX-100001", warehouse_id=wh.id, status="open")
    full_box = Box(box_number="BOX-100002", warehouse_id=wh.id, status="open")
    db.session.add_all([empty_box, full_box])
    db.session.commit()

    item = Nomenclature(sku="SKU-P1", barcode="6660000001", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    db.session.add(BoxItem(box_id=full_box.id, nomenclature_id=item.id, qty=1))
    db.session.commit()

    doc = PlacementDocument(number="RAZ-000001", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()

    resp = client_logged_in.get(f"/placement/{doc.id}")
    html = resp.get_data(as_text=True)

    assert "BOX-100001" not in html
    assert "BOX-100002" in html


def test_only_admin_can_edit_box_contents(db, client_logged_in):
    wh = _make_warehouse()
    box = Box(box_number="BOX-100003", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    item = Nomenclature(sku="SKU-P2", barcode="6660000002", name="Товар2", unit="шт")
    db.session.add(item)
    db.session.commit()

    # admin (client_logged_in по умолчанию) может добавить товар в короб
    resp = client_logged_in.post(
        f"/boxes/{box.id}/items/add",
        data={"nomenclature_id": item.id, "qty": 3},
        follow_redirects=True,
    )
    assert "Товар2" in resp.get_data(as_text=True)
    assert BoxItem.query.filter_by(box_id=box.id, nomenclature_id=item.id).first().qty == 3


def test_non_admin_cannot_edit_box_contents(db, client):
    wh = _make_warehouse()
    box = Box(box_number="BOX-100004", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    item = Nomenclature(sku="SKU-P3", barcode="6660000003", name="Товар3", unit="шт")
    db.session.add(item)
    db.session.commit()

    worker = User(username="worker", is_admin=False, role="warehouse")
    worker.set_password("password123")
    db.session.add(worker)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(worker.id)
        sess["_fresh"] = True

    resp = client.post(
        f"/boxes/{box.id}/items/add",
        data={"nomenclature_id": item.id, "qty": 1},
        follow_redirects=True,
    )

    assert "может только администратор" in resp.get_data(as_text=True)
    assert BoxItem.query.filter_by(box_id=box.id).count() == 0
