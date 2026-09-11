"""Перенос товара между коробами (boxes.move_item) — админская правка
состава коробов, аналогично add_item/update_item/delete_item: напрямую,
в обход документов, для случаев когда физически положили не в тот короб."""

from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, User, Warehouse


def _make_warehouse(code="WH-MOVE"):
    wh = Warehouse(code=code, name="Тест склад переноса")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(barcode, name="Товар переноса"):
    item = Nomenclature(sku=barcode, barcode=barcode, name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def test_move_full_qty_deletes_source_box_item(db, client_logged_in):
    wh = _make_warehouse("WH-MOVE-1")
    item = _make_item("3330000001")
    source = Box(box_number="BOX-MOVE-1S", warehouse_id=wh.id, status="open")
    target = Box(box_number="BOX-MOVE-1T", warehouse_id=wh.id, status="open")
    db.session.add_all([source, target])
    db.session.commit()
    box_item = BoxItem(box_id=source.id, nomenclature_id=item.id, qty=5)
    db.session.add(box_item)
    db.session.commit()

    client_logged_in.post(
        f"/boxes/{source.id}/items/{box_item.id}/move",
        data={"target_box_number": target.box_number, "qty": 5},
    )

    assert BoxItem.query.filter_by(box_id=source.id, nomenclature_id=item.id).first() is None
    target_item = BoxItem.query.filter_by(box_id=target.id, nomenclature_id=item.id).first()
    assert target_item.qty == 5


def test_move_partial_qty_keeps_remainder_in_source(db, client_logged_in):
    wh = _make_warehouse("WH-MOVE-2")
    item = _make_item("3330000002")
    source = Box(box_number="BOX-MOVE-2S", warehouse_id=wh.id, status="open")
    target = Box(box_number="BOX-MOVE-2T", warehouse_id=wh.id, status="open")
    db.session.add_all([source, target])
    db.session.commit()
    box_item = BoxItem(box_id=source.id, nomenclature_id=item.id, qty=10)
    db.session.add(box_item)
    db.session.commit()

    client_logged_in.post(
        f"/boxes/{source.id}/items/{box_item.id}/move",
        data={"target_box_number": target.box_number, "qty": 4},
    )

    assert BoxItem.query.filter_by(box_id=source.id, nomenclature_id=item.id).first().qty == 6
    assert BoxItem.query.filter_by(box_id=target.id, nomenclature_id=item.id).first().qty == 4


def test_move_merges_into_existing_target_item(db, client_logged_in):
    wh = _make_warehouse("WH-MOVE-3")
    item = _make_item("3330000003")
    source = Box(box_number="BOX-MOVE-3S", warehouse_id=wh.id, status="open")
    target = Box(box_number="BOX-MOVE-3T", warehouse_id=wh.id, status="open")
    db.session.add_all([source, target])
    db.session.commit()
    db.session.add(BoxItem(box_id=source.id, nomenclature_id=item.id, qty=3))
    db.session.add(BoxItem(box_id=target.id, nomenclature_id=item.id, qty=7))
    db.session.commit()
    source_item = BoxItem.query.filter_by(box_id=source.id, nomenclature_id=item.id).first()

    client_logged_in.post(
        f"/boxes/{source.id}/items/{source_item.id}/move",
        data={"target_box_number": target.box_number, "qty": 3},
    )

    assert BoxItem.query.filter_by(box_id=target.id, nomenclature_id=item.id).first().qty == 10


def test_move_rejects_qty_over_available(db, client_logged_in):
    wh = _make_warehouse("WH-MOVE-4")
    item = _make_item("3330000004")
    source = Box(box_number="BOX-MOVE-4S", warehouse_id=wh.id, status="open")
    target = Box(box_number="BOX-MOVE-4T", warehouse_id=wh.id, status="open")
    db.session.add_all([source, target])
    db.session.commit()
    box_item = BoxItem(box_id=source.id, nomenclature_id=item.id, qty=2)
    db.session.add(box_item)
    db.session.commit()

    resp = client_logged_in.post(
        f"/boxes/{source.id}/items/{box_item.id}/move",
        data={"target_box_number": target.box_number, "qty": 5},
        follow_redirects=True,
    )

    assert "Укажите корректное количество" in resp.get_data(as_text=True)
    assert BoxItem.query.filter_by(box_id=source.id, nomenclature_id=item.id).first().qty == 2


def test_move_rejects_unknown_target_box(db, client_logged_in):
    wh = _make_warehouse("WH-MOVE-5")
    item = _make_item("3330000005")
    source = Box(box_number="BOX-MOVE-5S", warehouse_id=wh.id, status="open")
    db.session.add(source)
    db.session.commit()
    box_item = BoxItem(box_id=source.id, nomenclature_id=item.id, qty=2)
    db.session.add(box_item)
    db.session.commit()

    resp = client_logged_in.post(
        f"/boxes/{source.id}/items/{box_item.id}/move",
        data={"target_box_number": "NOPE-000000", "qty": 2},
        follow_redirects=True,
    )

    assert "не найден" in resp.get_data(as_text=True)
    assert BoxItem.query.filter_by(box_id=source.id, nomenclature_id=item.id).first().qty == 2


def test_move_rejects_target_box_on_different_warehouse(db, client_logged_in):
    wh1 = _make_warehouse("WH-MOVE-6A")
    wh2 = _make_warehouse("WH-MOVE-6B")
    item = _make_item("3330000006")
    source = Box(box_number="BOX-MOVE-6S", warehouse_id=wh1.id, status="open")
    other_wh_box = Box(box_number="BOX-MOVE-6O", warehouse_id=wh2.id, status="open")
    db.session.add_all([source, other_wh_box])
    db.session.commit()
    box_item = BoxItem(box_id=source.id, nomenclature_id=item.id, qty=2)
    db.session.add(box_item)
    db.session.commit()

    resp = client_logged_in.post(
        f"/boxes/{source.id}/items/{box_item.id}/move",
        data={"target_box_number": other_wh_box.box_number, "qty": 2},
        follow_redirects=True,
    )

    assert "не найден" in resp.get_data(as_text=True)
    assert BoxItem.query.filter_by(box_id=source.id, nomenclature_id=item.id).first().qty == 2


def test_move_rejects_same_box_as_target(db, client_logged_in):
    wh = _make_warehouse("WH-MOVE-7")
    item = _make_item("3330000007")
    source = Box(box_number="BOX-MOVE-7S", warehouse_id=wh.id, status="open")
    db.session.add(source)
    db.session.commit()
    box_item = BoxItem(box_id=source.id, nomenclature_id=item.id, qty=2)
    db.session.add(box_item)
    db.session.commit()

    resp = client_logged_in.post(
        f"/boxes/{source.id}/items/{box_item.id}/move",
        data={"target_box_number": source.box_number, "qty": 2},
        follow_redirects=True,
    )

    assert "совпадает с исходным" in resp.get_data(as_text=True)
    assert BoxItem.query.filter_by(box_id=source.id, nomenclature_id=item.id).first().qty == 2


def test_non_admin_cannot_move_item(db, client):
    wh = _make_warehouse("WH-MOVE-8")
    item = _make_item("3330000008")
    source = Box(box_number="BOX-MOVE-8S", warehouse_id=wh.id, status="open")
    target = Box(box_number="BOX-MOVE-8T", warehouse_id=wh.id, status="open")
    db.session.add_all([source, target])
    db.session.commit()
    box_item = BoxItem(box_id=source.id, nomenclature_id=item.id, qty=2)
    db.session.add(box_item)
    db.session.commit()

    worker = User(username="worker-move", is_admin=False, role="warehouse")
    worker.set_password("password123")
    db.session.add(worker)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(worker.id)
        sess["_fresh"] = True

    client.post(
        f"/boxes/{source.id}/items/{box_item.id}/move",
        data={"target_box_number": target.box_number, "qty": 2},
    )

    assert BoxItem.query.filter_by(box_id=source.id, nomenclature_id=item.id).first().qty == 2
    assert BoxItem.query.filter_by(box_id=target.id, nomenclature_id=item.id).first() is None
