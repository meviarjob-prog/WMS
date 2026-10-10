"""Администратор может поправить количество в уже завершенной приемке (см.
чат: "для администратора добавь изменения кол-ва в завершенной приемке").
Обычным сотрудникам, как и раньше, после завершения менять qty нельзя —
только админу, и разница переносится на фактический остаток/короб, по
тому же принципу, что и смена товара в строке (см.
receiving.update_line_nomenclature)."""

from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, ReceivingDocument, ReceivingLine, UnplacedStock, User, Warehouse


def _make_warehouse(suffix):
    wh = Warehouse(code=f"WH-RQE{suffix}", name="Склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(suffix):
    item = Nomenclature(sku=f"SKU-RQE{suffix}", barcode=f"77707000{suffix}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _completed_doc_with_credited_line(client_logged_in, suffix, qty=10):
    """Обычная приемка без накладной: draft -> completed сразу, без
    пересчета/разбраковки — строка без короба зачисляется в остаток."""
    wh = _make_warehouse(suffix)
    item = _make_item(suffix)
    doc = ReceivingDocument(number=f"RQE-{suffix}", warehouse_id=wh.id, supplier="ИП Тестов")
    db.session.add(doc)
    db.session.commit()
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=qty)
    db.session.add(line)
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/complete")
    db.session.refresh(doc)
    db.session.refresh(line)
    return doc, line, wh, item


def test_admin_can_increase_qty_after_completion(db, client_logged_in):
    doc, line, wh, item = _completed_doc_with_credited_line(client_logged_in, "1", qty=10)
    assert doc.status == "completed"
    assert UnplacedStock.available(wh.id, item.id) == 10

    resp = client_logged_in.post(
        f"/receiving/{doc.id}/lines/{line.id}/update", data={"qty": "15"}, follow_redirects=True
    )

    assert resp.status_code == 200
    db.session.refresh(line)
    assert line.qty == 15
    assert UnplacedStock.available(wh.id, item.id) == 15


def test_admin_can_decrease_qty_after_completion(db, client_logged_in):
    doc, line, wh, item = _completed_doc_with_credited_line(client_logged_in, "2", qty=10)

    resp = client_logged_in.post(
        f"/receiving/{doc.id}/lines/{line.id}/update", data={"qty": "6"}, follow_redirects=True
    )

    assert resp.status_code == 200
    db.session.refresh(line)
    assert line.qty == 6
    assert UnplacedStock.available(wh.id, item.id) == 6


def test_decrease_blocked_once_already_placed(db, client_logged_in):
    doc, line, wh, item = _completed_doc_with_credited_line(client_logged_in, "3", qty=10)
    # Часть уже разместили в короб (как делает "Размещение").
    UnplacedStock.consume(wh.id, item.id, 4)
    assert UnplacedStock.available(wh.id, item.id) == 6

    resp = client_logged_in.post(
        f"/receiving/{doc.id}/lines/{line.id}/update", data={"qty": "3"}, follow_redirects=True
    )

    assert resp.status_code == 200
    assert "уже частично размещен" in resp.get_data(as_text=True)
    db.session.refresh(line)
    assert line.qty == 10  # не изменилось
    assert UnplacedStock.available(wh.id, item.id) == 6


def test_non_admin_cannot_edit_qty_after_completion(db, client):
    wh = _make_warehouse("4")
    item = _make_item("4")
    user = User(username="staffer-rqe", full_name="Складской", role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    # Владелец документа — иначе _restrict_document_access скрыл бы его
    # как чужой (404) раньше, чем код дошел бы до проверки статуса.
    doc = ReceivingDocument(
        number="RQE-4", warehouse_id=wh.id, supplier="ИП Тестов", status="completed", created_by_id=user.id,
    )
    db.session.add(doc)
    db.session.commit()
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10)
    db.session.add(line)
    db.session.commit()
    UnplacedStock.add(wh.id, item.id, 10)

    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True

    resp = client.post(f"/receiving/{doc.id}/lines/{line.id}/update", data={"qty": "20"}, follow_redirects=True)

    assert "уже завершен" in resp.get_data(as_text=True)
    db.session.refresh(line)
    assert line.qty == 10
    assert UnplacedStock.available(wh.id, item.id) == 10


def test_admin_can_edit_box_line_qty_after_completion(db, client_logged_in):
    wh = _make_warehouse("5")
    item = _make_item("5")
    resp = client_logged_in.post(
        "/receiving/new", data={"warehouse_id": wh.id, "supplier": ""}, follow_redirects=True
    )
    doc_id = int(resp.request.path.rstrip("/").rsplit("/", 1)[-1])
    client_logged_in.post(f"/receiving/{doc_id}/boxes/create")
    box = Box.query.filter_by(warehouse_id=wh.id).order_by(Box.id.desc()).first()
    client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box.id}/lines/add-by-barcode",
        json={"barcode": item.barcode, "qty": 10},
    )
    client_logged_in.post(f"/receiving/{doc_id}/complete")
    line = ReceivingLine.query.filter_by(document_id=doc_id, box_id=box.id).first()
    assert line is not None and line.qty == 10

    resp = client_logged_in.post(
        f"/receiving/{doc_id}/lines/{line.id}/update", data={"qty": "7"}, follow_redirects=True
    )

    assert resp.status_code == 200
    db.session.refresh(line)
    assert line.qty == 7
    box_item = BoxItem.query.filter_by(box_id=box.id, nomenclature_id=item.id).first()
    assert box_item.qty == 7


def test_detail_page_shows_edit_form_for_admin_only_on_completed(db, client_logged_in):
    doc, line, wh, item = _completed_doc_with_credited_line(client_logged_in, "6", qty=10)

    admin_html = client_logged_in.get(f"/receiving/{doc.id}").get_data(as_text=True)
    assert f"/receiving/{doc.id}/lines/{line.id}/update" in admin_html

    user = User(username="staffer-rqe6", full_name="Складской", role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    # Владелец документа — иначе _restrict_document_access скрыл бы его
    # как чужой (404) раньше, чем страница вообще отрендерилась бы.
    doc.created_by_id = user.id
    db.session.commit()
    from flask import g

    with client_logged_in.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    g.pop("_login_user", None)

    staff_html = client_logged_in.get(f"/receiving/{doc.id}").get_data(as_text=True)
    assert f"/receiving/{doc.id}/lines/{line.id}/update" not in staff_html
