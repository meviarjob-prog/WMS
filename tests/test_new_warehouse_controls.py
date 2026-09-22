from datetime import datetime

from wms.extensions import db
from wms.models import (
    AppSetting, Box, BoxItem, InventoryDocument, MovementDocument, MovementLine,
    Nomenclature, OneCQuantityCheck, ReceivingDocument, UnplacedStock,
    UnplacedStockLot, User, Warehouse,
)


def _staff(name, warehouse=None):
    user = User(username=name, warehouse_id=warehouse.id if warehouse else None)
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def test_assigned_employee_cannot_move_box_from_another_warehouse(db, client):
    own = Warehouse(code="OWN", name="Свой")
    other = Warehouse(code="OTHER", name="Другой")
    target = Warehouse(code="TARGET", name="Назначение")
    db.session.add_all([own, other, target])
    db.session.commit()
    user = _staff("assigned-worker", own)
    box = Box(box_number="FOREIGN-BOX", warehouse_id=other.id)
    db.session.add(box)
    db.session.commit()
    _login(client, user)

    client.post("/movement/route-box/add", data={"box_id": box.id, "to_warehouse_id": target.id})

    assert MovementDocument.query.count() == 0


def test_admin_can_choose_sender_even_when_admin_has_assigned_warehouse(
    db, client_logged_in, admin_user
):
    assigned = Warehouse(code="ADM-A", name="Назначенный админу")
    selected = Warehouse(code="ADM-B", name="Выбранный отправитель")
    target = Warehouse(code="ADM-C", name="Получатель")
    db.session.add_all([assigned, selected, target])
    db.session.commit()
    admin_user.warehouse_id = assigned.id
    db.session.commit()

    client_logged_in.post(
        "/movement/new",
        data={"from_warehouse_id": selected.id, "to_warehouse_id": target.id},
    )

    assert MovementDocument.query.one().from_warehouse_id == selected.id


def test_admin_can_edit_sender_before_dispatch(db, client_logged_in):
    old = Warehouse(code="EDIT-A", name="Старый отправитель")
    new = Warehouse(code="EDIT-B", name="Новый отправитель")
    target = Warehouse(code="EDIT-C", name="Получатель")
    db.session.add_all([old, new, target])
    db.session.commit()
    doc = MovementDocument(number="PER-EDIT", from_warehouse_id=old.id, to_warehouse_id=target.id)
    db.session.add(doc)
    db.session.commit()

    client_logged_in.post(
        f"/movement/{doc.id}/change-sender", data={"from_warehouse_id": new.id}
    )

    assert MovementDocument.query.get(doc.id).from_warehouse_id == new.id


def test_admin_cannot_change_sender_while_boxes_are_on_another_warehouse(
    db, client_logged_in
):
    old = Warehouse(code="LOCK-A", name="Фактический склад")
    new = Warehouse(code="LOCK-B", name="Неверный склад")
    target = Warehouse(code="LOCK-C", name="Получатель")
    db.session.add_all([old, new, target])
    db.session.commit()
    box = Box(box_number="LOCK-BOX", warehouse_id=old.id)
    doc = MovementDocument(number="PER-LOCK", from_warehouse_id=old.id, to_warehouse_id=target.id)
    db.session.add_all([box, doc])
    db.session.commit()
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=old.id))
    db.session.commit()

    client_logged_in.post(
        f"/movement/{doc.id}/change-sender", data={"from_warehouse_id": new.id}
    )

    assert MovementDocument.query.get(doc.id).from_warehouse_id == old.id


def test_thirtieth_ozon_box_closes_document_and_next_box_starts_new(db, client_logged_in):
    sender = Warehouse(code="S30", name="Основной")
    target = Warehouse(code="O30", name="Краснодар", marketplace="ozon")
    db.session.add_all([sender, target])
    db.session.commit()
    boxes = [Box(box_number=f"OZ-{i:03}", warehouse_id=sender.id) for i in range(31)]
    db.session.add_all(boxes)
    db.session.commit()

    for box in boxes:
        client_logged_in.post("/movement/route-box/add", data={"box_id": box.id, "to_warehouse_id": target.id})

    docs = MovementDocument.query.order_by(MovementDocument.id).all()
    assert len(docs) == 2
    assert docs[0].status == "collected"
    assert docs[0].lines.count() == 30
    assert docs[1].status == "draft"
    assert docs[1].lines.count() == 1


def test_split_moves_selected_boxes_to_new_document(db, client_logged_in):
    sender = Warehouse(code="SS", name="Отправитель")
    target = Warehouse(code="ST", name="Получатель")
    db.session.add_all([sender, target])
    db.session.commit()
    doc = MovementDocument(number="PER-SPLIT", from_warehouse_id=sender.id, to_warehouse_id=target.id)
    db.session.add(doc)
    db.session.commit()
    boxes = [Box(box_number=f"SPL-{i}", warehouse_id=sender.id) for i in range(3)]
    db.session.add_all(boxes)
    db.session.commit()
    lines = [MovementLine(document_id=doc.id, box_id=b.id, from_warehouse_id=sender.id) for b in boxes]
    db.session.add_all(lines)
    db.session.commit()

    client_logged_in.post(f"/movement/{doc.id}/split", data={"line_ids": [str(lines[0].id)]})

    assert doc.lines.count() == 2
    other = MovementDocument.query.filter(MovementDocument.id != doc.id).one()
    assert other.lines.count() == 1


def test_inventory_empty_box_opens_receiving_and_returns_to_inventory(db, client_logged_in):
    warehouse = Warehouse(code="INV-R", name="Основной")
    db.session.add(warehouse)
    db.session.commit()
    from wms.models import Cell
    cell = Cell(code="A-01", warehouse_id=warehouse.id)
    box = Box(box_number="EMPTY-I", warehouse_id=warehouse.id)
    db.session.add_all([cell, box])
    db.session.commit()
    inventory = InventoryDocument(number="INV-RETURN", warehouse_id=warehouse.id, cell_id=cell.id)
    db.session.add(inventory)
    db.session.commit()

    response = client_logged_in.post(
        f"/inventory/{inventory.id}/boxes/add", data={"box_number": box.box_number}, follow_redirects=True
    )
    assert "Сделать приемку в короб" in response.get_data(as_text=True)

    client_logged_in.post(f"/inventory/{inventory.id}/empty-box/{box.id}/receive")
    receiving = ReceivingDocument.query.one()
    assert receiving.return_inventory_id == inventory.id
    assert receiving.return_inventory_box_id == box.id


def test_admin_can_clear_unplaced_stock_and_lots(db, client_logged_in):
    warehouse = Warehouse(code="CLR", name="Склад очистки")
    item = Nomenclature(sku="CLR", barcode="100", name="Товар", unit="шт")
    db.session.add_all([warehouse, item])
    db.session.commit()
    UnplacedStock.add(warehouse.id, item.id, 7)
    db.session.commit()

    client_logged_in.post("/placement/unplaced/clear", data={"warehouse_id": warehouse.id})

    assert UnplacedStock.query.filter_by(warehouse_id=warehouse.id).count() == 0
    assert UnplacedStockLot.query.filter_by(warehouse_id=warehouse.id).one().qty_remaining == 0


def test_1c_confirmation_records_and_clears_quantity_mismatch(db, client_logged_in):
    db.session.add(AppSetting(key="api_1c_token", value="test-token"))
    db.session.commit()
    payload = {
        "quantity_checks": [{
            "document_type": "movement", "document_id": 42,
            "document_number": "PER-42", "barcode": "123", "name": "Товар",
            "wms_qty": 10, "one_c_qty": 9,
        }]
    }
    response = client_logged_in.post(
        "/integrations/1c/api/export/confirm", json=payload,
        headers={"X-1C-Token": "test-token"},
    )
    assert response.status_code == 200
    assert OneCQuantityCheck.query.one().diff() == -1

    payload["quantity_checks"][0]["one_c_qty"] = 10
    client_logged_in.post(
        "/integrations/1c/api/export/confirm", json=payload,
        headers={"X-1C-Token": "test-token"},
    )
    assert OneCQuantityCheck.query.count() == 0


def test_movement_shortage_reduces_physical_box_stock(db, client_logged_in):
    sender = Warehouse(code="R-S", name="Отправитель")
    target = Warehouse(code="R-T", name="Ozon", marketplace="ozon")
    item = Nomenclature(sku="R-I", barcode="555", name="Товар недовоза", unit="шт")
    db.session.add_all([sender, target, item])
    db.session.commit()
    box = Box(box_number="R-BOX", warehouse_id=target.id)
    db.session.add(box)
    db.session.commit()
    box_item = BoxItem(box_id=box.id, nomenclature_id=item.id, qty=10)
    doc = MovementDocument(
        number="PER-R", from_warehouse_id=sender.id, to_warehouse_id=target.id,
        status="completed", completed_at=datetime.utcnow(),
        marketplace_request_number="REQ-PER-R",
        marketplace_request_created_at=datetime.utcnow(), sent_qty_snapshot=10,
    )
    db.session.add_all([box_item, doc])
    db.session.commit()
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
    db.session.commit()

    client_logged_in.post(f"/movement/{doc.id}/receive", data={f"qty_{item.id}": "7"})

    assert BoxItem.query.get(box_item.id).qty == 7
    assert MovementDocument.query.get(doc.id).total_sent_qty() == 10
    assert MovementDocument.query.get(doc.id).total_received_qty() == 7
