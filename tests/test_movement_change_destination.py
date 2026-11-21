"""Массовая смена склада назначения (movement.change_destination) —
только администратор; нужна, чтобы переделать перемещения WB на Ozon."""

from datetime import datetime

from wms.extensions import db
from wms.models import (
    Box, BoxItem, MovementCargoPlace, MovementDocument, MovementLine, Nomenclature, User, Warehouse,
)


def _setup(n_boxes=2, status="draft"):
    sender = Warehouse(code="WH-CD0", name="Основной")
    wb = Warehouse(code="WH-CD1", name="ВБ: Коледино", marketplace="wb", marketplace_city="Коледино")
    ozon = Warehouse(code="WH-CD2", name="ОЗОН: Тверь", marketplace="ozon", marketplace_city="Тверь")
    db.session.add_all([sender, wb, ozon])
    db.session.commit()
    item = Nomenclature(sku="CD-1", barcode="CD0001", name="Шапка", unit="шт")
    db.session.add(item)
    db.session.commit()
    doc = MovementDocument(number="PER-CD1", from_warehouse_id=sender.id, to_warehouse_id=wb.id, status=status)
    db.session.add(doc)
    db.session.commit()
    boxes = []
    for i in range(n_boxes):
        box = Box(box_number=f"BOX-CD{i}", warehouse_id=wb.id if status == "completed" else sender.id,
                  status="open")
        db.session.add(box)
        db.session.commit()
        db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=5))
        db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
        boxes.append(box)
    db.session.commit()
    return doc, sender, wb, ozon, boxes


def _change(client, doc_ids, warehouse_id):
    return client.post("/movement/change-destination",
                       data={"doc_ids": doc_ids, "new_to_warehouse_id": warehouse_id}, follow_redirects=True)


def test_admin_changes_draft_from_wb_to_ozon(db, client_logged_in):
    doc, sender, wb, ozon, boxes = _setup()
    doc.marketplace_request_number = "WB-REQ-1"
    doc.marketplace_request_created_at = datetime.utcnow()
    db.session.add(MovementCargoPlace(document_id=doc.id, box_id=boxes[0].id, box_number="BOX-CD0", cargo_barcode="GM1"))
    db.session.commit()

    resp = _change(client_logged_in, [doc.id], ozon.id)
    assert "Склад назначения изменен" in resp.get_data(as_text=True)
    doc = MovementDocument.query.get(doc.id)
    assert doc.to_warehouse_id == ozon.id
    assert doc.marketplace_request_number is None and doc.marketplace_request_created_at is None
    assert MovementCargoPlace.query.count() == 0


def test_completed_unreceived_document_moves_boxes_to_new_warehouse(db, client_logged_in):
    doc, sender, wb, ozon, boxes = _setup(status="completed")
    _change(client_logged_in, [doc.id], ozon.id)
    assert MovementDocument.query.get(doc.id).to_warehouse_id == ozon.id
    for box in boxes:
        assert Box.query.get(box.id).warehouse_id == ozon.id


def test_received_merged_and_same_destination_are_skipped(db, client_logged_in):
    doc, sender, wb, ozon, boxes = _setup()
    received = MovementDocument(number="PER-CD2", from_warehouse_id=sender.id, to_warehouse_id=wb.id,
                                status="completed", received_at=datetime.utcnow())
    merged = MovementDocument(number="PER-CD3", from_warehouse_id=sender.id, to_warehouse_id=wb.id, status="merged")
    db.session.add_all([received, merged])
    db.session.commit()
    resp = _change(client_logged_in, [received.id, merged.id, doc.id], wb.id)
    html = resp.get_data(as_text=True)
    assert "уже на этот склад" in html
    resp = _change(client_logged_in, [received.id, merged.id], ozon.id)
    html = resp.get_data(as_text=True)
    assert "уже принят" in html and "объединен" in html
    assert MovementDocument.query.get(received.id).to_warehouse_id == wb.id
    assert MovementDocument.query.get(merged.id).to_warehouse_id == wb.id


def test_ozon_destination_splits_big_document_into_chunks_of_30(db, client_logged_in):
    doc, sender, wb, ozon, boxes = _setup(n_boxes=65, status="completed")
    doc.shipped_at = datetime.utcnow()
    doc.completed_at = datetime.utcnow()
    db.session.commit()
    resp = _change(client_logged_in, [doc.id], ozon.id)
    html = resp.get_data(as_text=True)
    assert "из PER-CD1, 30 кор." in html and "из PER-CD1, 5 кор." in html

    docs = MovementDocument.query.order_by(MovementDocument.id).all()
    assert [d.lines.count() for d in docs] == [30, 30, 5]
    assert {d.to_warehouse_id for d in docs} == {ozon.id}
    assert {d.status for d in docs} == {"completed"}
    assert all(d.shipped_at is not None for d in docs)
    # все короба на Ozon-складе, ни один не потерян и не задвоен
    all_box_ids = [line.box_id for d in docs for line in d.lines]
    assert len(all_box_ids) == len(set(all_box_ids)) == 65
    assert all(Box.query.get(i).warehouse_id == ozon.id for i in all_box_ids)


def test_split_draft_marks_full_chunks_as_collected(db, client_logged_in):
    doc, sender, wb, ozon, boxes = _setup(n_boxes=45)
    _change(client_logged_in, [doc.id], ozon.id)
    docs = MovementDocument.query.order_by(MovementDocument.id).all()
    assert [(d.lines.count(), d.status) for d in docs] == [(30, "collected"), (15, "draft")]


def test_big_document_synced_to_1c_is_not_split(db, client_logged_in):
    doc, sender, wb, ozon, boxes = _setup(n_boxes=40)
    doc.synced_to_1c_at = datetime.utcnow()
    db.session.commit()
    resp = _change(client_logged_in, [doc.id], ozon.id)
    assert "дробить нельзя" in resp.get_data(as_text=True)
    assert MovementDocument.query.count() == 1
    assert MovementDocument.query.get(doc.id).to_warehouse_id == wb.id


def test_split_keeps_author_and_moves_pick_events(db, client_logged_in, admin_user):
    from wms.models import WorkEvent

    doc, sender, wb, ozon, boxes = _setup(n_boxes=32)
    doc.created_by_id = admin_user.id
    for box in boxes:
        db.session.add(WorkEvent(user_id=admin_user.id, work_date=datetime.utcnow().date(), kind="pick",
                                 box_id=box.id, qty=5, ref_kind="movement", ref_id=doc.id))
    db.session.commit()
    _change(client_logged_in, [doc.id], ozon.id)
    parts = MovementDocument.query.order_by(MovementDocument.id).all()
    assert {d.created_by_id for d in parts} == {admin_user.id}
    for d in parts:
        box_ids = {line.box_id for line in d.lines}
        events = WorkEvent.query.filter_by(kind="pick", ref_id=d.id).all()
        assert {e.box_id for e in events} == box_ids


def test_cannot_pick_sender_as_destination(db, client_logged_in):
    doc, sender, wb, ozon, boxes = _setup()
    resp = _change(client_logged_in, [doc.id], sender.id)
    assert "совпал бы с отправителем" in resp.get_data(as_text=True)
    assert MovementDocument.query.get(doc.id).to_warehouse_id == wb.id


def test_synced_to_1c_document_gets_manual_fix_warning(db, client_logged_in):
    doc, sender, wb, ozon, boxes = _setup()
    doc.synced_to_1c_at = datetime.utcnow()
    db.session.commit()
    resp = _change(client_logged_in, [doc.id], ozon.id)
    html = resp.get_data(as_text=True)
    assert "поправьте вручную" in html and "PER-CD1" in html
    assert MovementDocument.query.get(doc.id).to_warehouse_id == ozon.id


def test_only_admin_can_change_destination(db, client):
    doc, sender, wb, ozon, boxes = _setup()
    worker = User(username="cd_user", full_name="Не админ", role="warehouse")
    worker.set_password("pw12345")
    db.session.add(worker)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(worker.id)
        sess["_fresh"] = True
    _change(client, [doc.id], ozon.id)
    assert MovementDocument.query.get(doc.id).to_warehouse_id == wb.id


def test_other_admin_users_cannot_change_destination(db, client):
    doc, sender, wb, ozon, boxes = _setup()
    other = User(username="boss2", full_name="Другой админ", is_admin=True)
    other.set_password("pw12345")
    db.session.add(other)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(other.id)
        sess["_fresh"] = True
    _change(client, [doc.id], ozon.id)
    assert MovementDocument.query.get(doc.id).to_warehouse_id == wb.id
    assert "Сменить склад получатель" not in client.get("/movement/").get_data(as_text=True)


def test_list_shows_admin_controls(db, client_logged_in):
    doc, sender, wb, ozon, boxes = _setup()
    html = client_logged_in.get("/movement/").get_data(as_text=True)
    assert "Сменить склад получатель" in html and "ОЗОН · ОЗОН: Тверь" in html
