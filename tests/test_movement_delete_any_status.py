"""Администратор может удалить перемещение в любом статусе, включая
завершенные (короба возвращаются туда, где были до перемещения) и
объединенные (см. movement.delete_document, movement.merge_documents)."""

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, MovementLine, Nomenclature, ShipmentPlan, ShipmentPlanLine, Warehouse


def _make_warehouses(suffix):
    sender = Warehouse(code=f"WH-DEL{suffix}A", name="Склад-отправитель")
    dest = Warehouse(code=f"WH-DEL{suffix}B", name="ОЗОН: Город")
    db.session.add_all([sender, dest])
    db.session.commit()
    return sender, dest


def _make_item(suffix):
    item = Nomenclature(sku=f"SKU-DEL{suffix}", barcode=f"77702000{suffix}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_doc_with_box(sender, dest, item, number, box_number, qty=5):
    doc = MovementDocument(number=number, from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()
    box = Box(box_number=box_number, warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
    db.session.add(
        MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id, from_cell_id=box.cell_id)
    )
    db.session.commit()
    return doc, box


def test_admin_can_delete_completed_document_and_box_reverts(db, client_logged_in):
    sender, dest = _make_warehouses("1")
    item = _make_item("1")
    doc, box = _make_doc_with_box(sender, dest, item, "PER-DEL-0001", "BOX-DEL101")

    client_logged_in.post(f"/movement/{doc.id}/complete")
    box = Box.query.get(box.id)
    assert box.warehouse_id == dest.id

    resp = client_logged_in.post(f"/movement/{doc.id}/delete", follow_redirects=True)

    assert resp.status_code == 200
    assert MovementDocument.query.get(doc.id) is None
    box = Box.query.get(box.id)
    assert box.warehouse_id == sender.id


def test_admin_can_delete_completed_and_received_document_reverts_shipment_fulfillment(db, client_logged_in):
    sender, dest = _make_warehouses("2")
    item = _make_item("2")
    doc, box = _make_doc_with_box(sender, dest, item, "PER-DEL-0002", "BOX-DEL102", qty=7)
    plan = ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    db.session.commit()
    plan_line = ShipmentPlanLine(
        plan_id=plan.id, warehouse_id=dest.id, nomenclature_id=item.id, barcode=item.barcode, planned_qty=100, fulfilled_qty=0
    )
    db.session.add(plan_line)
    db.session.commit()

    client_logged_in.post(f"/movement/{doc.id}/complete")
    doc.marketplace_request_number = "REQ-DEL-2"
    db.session.commit()
    client_logged_in.post(f"/movement/{doc.id}/mark-marketplace-request")
    client_logged_in.post(f"/movement/{doc.id}/receive")
    plan_line = ShipmentPlanLine.query.get(plan_line.id)
    assert plan_line.fulfilled_qty == 7

    client_logged_in.post(f"/movement/{doc.id}/delete")

    plan_line = ShipmentPlanLine.query.get(plan_line.id)
    assert plan_line.fulfilled_qty == 0


def test_admin_can_delete_merged_source_document(db, client_logged_in):
    sender, dest = _make_warehouses("3")
    item = _make_item("3")
    doc1, box1 = _make_doc_with_box(sender, dest, item, "PER-DEL-0003", "BOX-DEL103")
    doc2, box2 = _make_doc_with_box(sender, dest, item, "PER-DEL-0004", "BOX-DEL104")

    client_logged_in.post("/movement/merge", data={"doc_ids": [doc1.id, doc2.id]})
    doc1 = MovementDocument.query.get(doc1.id)
    assert doc1.status == "merged"

    resp = client_logged_in.post(f"/movement/{doc1.id}/delete", follow_redirects=True)

    assert resp.status_code == 200
    assert MovementDocument.query.get(doc1.id) is None
    assert MovementDocument.query.get(doc2.id) is not None


def test_admin_can_delete_merge_target_and_sources_become_plain_drafts(db, client_logged_in):
    sender, dest = _make_warehouses("4")
    item = _make_item("4")
    doc1, box1 = _make_doc_with_box(sender, dest, item, "PER-DEL-0005", "BOX-DEL105")
    doc2, box2 = _make_doc_with_box(sender, dest, item, "PER-DEL-0006", "BOX-DEL106")

    client_logged_in.post("/movement/merge", data={"doc_ids": [doc1.id, doc2.id]})
    doc1 = MovementDocument.query.get(doc1.id)
    merged_id = doc1.merged_into_id

    client_logged_in.post(f"/movement/{merged_id}/delete")

    assert MovementDocument.query.get(merged_id) is None
    doc1 = MovementDocument.query.get(doc1.id)
    doc2 = MovementDocument.query.get(doc2.id)
    assert doc1.status == "draft"
    assert doc1.merged_into_id is None
    assert doc2.status == "draft"
    assert doc2.merged_into_id is None


def test_delete_document_still_requires_admin(db, client):
    from wms.models import User

    sender, dest = _make_warehouses("5")
    item = _make_item("5")
    doc, _ = _make_doc_with_box(sender, dest, item, "PER-DEL-0007", "BOX-DEL107")
    user = User(username="staffer-del", full_name="Складской", role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True

    client.post(f"/movement/{doc.id}/delete")

    assert MovementDocument.query.get(doc.id) is not None
