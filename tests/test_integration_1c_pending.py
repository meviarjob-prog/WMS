"""Очередь выгрузки в 1С (/integrations/1c/pending) — списки документов,
которые уйдут в 1С при следующей синхронизации, и ручное исключение
конкретного документа из очереди (accounting_entered_at), например когда
бухгалтер уже внес его в 1С сам."""

from datetime import datetime

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    InventoryDocument,
    InventoryLine,
    MovementDocument,
    MovementLine,
    Nomenclature,
    ReceivingDocument,
    ReceivingLine,
    SupplierReturn,
    User,
    Warehouse,
)


def _make_item(barcode, name="Товар для очереди"):
    item = Nomenclature(sku=barcode, barcode=barcode, name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_staff_user():
    user = User(username="pending-staffer", full_name="Складской", role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    return user


def _login_as(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _make_pending_movement(number="PER-PEND-1", from_code="WH-PEND-A", to_code="WH-PEND-B"):
    sender = Warehouse(code=from_code, name="Основной склад")
    receiver = Warehouse(code=to_code, name="ОЗОН: Тест", marketplace="ozon", marketplace_city="Тест")
    db.session.add_all([sender, receiver])
    db.session.commit()
    item = _make_item(f"777{number}")
    box = Box(box_number=f"BOX-{number}", warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=2))
    doc = MovementDocument(
        number=number,
        from_warehouse_id=sender.id,
        to_warehouse_id=receiver.id,
        status="completed",
        marketplace_request_created_at=datetime.utcnow(),
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id, from_cell_id=box.cell_id)
    )
    db.session.commit()
    return doc


def _make_pending_inventory(number="INV-PEND-1"):
    wh = Warehouse(code=f"WH-{number}", name="Склад для инвентаризации")
    db.session.add(wh)
    db.session.commit()
    doc = InventoryDocument(number=number, warehouse_id=wh.id, status="completed")
    db.session.add(doc)
    db.session.commit()
    item = _make_item(f"888{number}")
    db.session.add(InventoryLine(document_id=doc.id, nomenclature_id=item.id, qty=1))
    db.session.commit()
    return doc


def _make_pending_receiving_adjustment(number="НАКЛ-PEND-1"):
    wh = Warehouse(code=f"WH-{number}", name="Склад приемки")
    db.session.add(wh)
    db.session.commit()
    doc = ReceivingDocument(
        number=number, warehouse_id=wh.id, invoice_file_name="накладная.xlsx", status="completed"
    )
    db.session.add(doc)
    db.session.commit()
    item = _make_item(f"999{number}")
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=5, expected_qty=8))
    db.session.commit()
    return doc


def _make_pending_supplier_return_group(invoice_number="НАКЛ-RET-1"):
    wh = Warehouse(code=f"WH-{invoice_number}", name="Склад возврата")
    db.session.add(wh)
    db.session.commit()
    doc = ReceivingDocument(
        number=invoice_number, warehouse_id=wh.id, invoice_file_name="накладная.xlsx", supplier="ИП Тестов"
    )
    db.session.add(doc)
    db.session.commit()
    item1 = _make_item(f"111{invoice_number}", "Товар возврата 1")
    item2 = _make_item(f"222{invoice_number}", "Товар возврата 2")
    db.session.add_all(
        [
            SupplierReturn(
                warehouse_id=wh.id,
                nomenclature_id=item1.id,
                qty=2,
                receiving_document_id=doc.id,
                supplier_name="ИП Тестов",
                invoice_number=invoice_number,
            ),
            SupplierReturn(
                warehouse_id=wh.id,
                nomenclature_id=item2.id,
                qty=1,
                receiving_document_id=doc.id,
                supplier_name="ИП Тестов",
                invoice_number=invoice_number,
            ),
        ]
    )
    db.session.commit()
    return doc


def test_pending_requires_admin(db, client):
    user = _make_staff_user()
    _login_as(client, user)
    resp = client.get("/integrations/1c/pending", follow_redirects=True)
    assert "Доступно только администратору" in resp.get_data(as_text=True)


def test_pending_lists_movement(db, client_logged_in):
    doc = _make_pending_movement()
    html = client_logged_in.get("/integrations/1c/pending").get_data(as_text=True)
    assert doc.number in html


def test_toggle_movement_excludes_and_restores(db, client_logged_in):
    doc = _make_pending_movement("PER-PEND-2")

    resp = client_logged_in.post(
        f"/integrations/1c/pending/movement/{doc.id}/toggle", follow_redirects=True
    )
    assert "убрано из очереди выгрузки" in resp.get_data(as_text=True)
    db.session.refresh(doc)
    assert doc.accounting_entered_at is not None
    html = client_logged_in.get("/integrations/1c/pending").get_data(as_text=True)
    assert doc.number not in html

    resp = client_logged_in.post(
        f"/integrations/1c/pending/movement/{doc.id}/toggle", follow_redirects=True
    )
    assert "возвращено в очередь" in resp.get_data(as_text=True)
    db.session.refresh(doc)
    assert doc.accounting_entered_at is None
    html = client_logged_in.get("/integrations/1c/pending").get_data(as_text=True)
    assert doc.number in html


def test_excluded_movement_is_not_exported(db, client_logged_in):
    from wms.models import AppSetting

    db.session.add(AppSetting(key="api_1c_token", value="tok"))
    db.session.commit()
    doc = _make_pending_movement("PER-PEND-3")
    client_logged_in.post(f"/integrations/1c/pending/movement/{doc.id}/toggle")

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": "tok"})
    numbers = [m["number"] for m in resp.get_json()["movements"]]
    assert doc.number not in numbers


def test_pending_lists_inventory_and_toggle_excludes(db, client_logged_in):
    doc = _make_pending_inventory()
    html = client_logged_in.get("/integrations/1c/pending").get_data(as_text=True)
    assert doc.number in html

    client_logged_in.post(f"/integrations/1c/pending/inventory/{doc.id}/toggle", follow_redirects=True)
    db.session.refresh(doc)
    assert doc.accounting_entered_at is not None
    html = client_logged_in.get("/integrations/1c/pending").get_data(as_text=True)
    assert doc.number not in html


def test_pending_lists_receiving_adjustment_and_toggle_excludes(db, client_logged_in):
    doc = _make_pending_receiving_adjustment()
    html = client_logged_in.get("/integrations/1c/pending").get_data(as_text=True)
    assert doc.number in html

    client_logged_in.post(
        f"/integrations/1c/pending/receiving-adjustment/{doc.id}/toggle", follow_redirects=True
    )
    db.session.refresh(doc)
    assert doc.accounting_entered_at is not None
    html = client_logged_in.get("/integrations/1c/pending").get_data(as_text=True)
    assert doc.number not in html


def test_pending_lists_supplier_return_group_and_toggle_excludes_both_lines(db, client_logged_in):
    doc = _make_pending_supplier_return_group("НАКЛ-RET-2")
    html = client_logged_in.get("/integrations/1c/pending").get_data(as_text=True)
    assert "НАКЛ-RET-2" in html

    resp = client_logged_in.post(
        f"/integrations/1c/pending/supplier-return/{doc.id}/toggle", follow_redirects=True
    )
    assert "убран из очереди выгрузки" in resp.get_data(as_text=True)

    returns = SupplierReturn.query.filter_by(receiving_document_id=doc.id).all()
    assert len(returns) == 2
    assert all(r.accounting_entered_at is not None for r in returns)

    html = client_logged_in.get("/integrations/1c/pending").get_data(as_text=True)
    assert "НАКЛ-RET-2" not in html

    resp = client_logged_in.post(
        f"/integrations/1c/pending/supplier-return/{doc.id}/toggle", follow_redirects=True
    )
    assert "возвращен в очередь" in resp.get_data(as_text=True)
    returns = SupplierReturn.query.filter_by(receiving_document_id=doc.id).all()
    assert all(r.accounting_entered_at is None for r in returns)


def test_non_admin_cannot_toggle(db, client):
    doc = _make_pending_movement("PER-PEND-4")
    user = _make_staff_user()
    _login_as(client, user)
    client.post(f"/integrations/1c/pending/movement/{doc.id}/toggle")
    db.session.refresh(doc)
    assert doc.accounting_entered_at is None
