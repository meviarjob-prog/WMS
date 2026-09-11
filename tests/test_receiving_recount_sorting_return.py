"""Приемка теперь идет в три этапа вместо одной кнопки "Завершить приемку":
draft -> (Отправить на пересчет) -> recounting -> (Отправить на разбраковку)
-> sorting -> (Завершить приемку) -> completed.

На "Пересчете" можно поправить кол-во по строке, если оно разошлось с тем,
что внесли при самой приемке. На "Разбраковке" по каждой строке (кроме уже
упакованных в короб при приемке — они разбраковке не подлежат) выделяется
кол-во брака: остальное уходит в неразмещенный остаток как обычно, а брак —
отдельным SupplierReturn, привязанным к этой приемке (supplier_name/
invoice_number — снимок для будущей синхронизации с 1С)."""

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    Nomenclature,
    ReceivingDocument,
    ReceivingLine,
    SupplierReturn,
    UnplacedStock,
    User,
    Warehouse,
)


def _make_warehouse(code="WH-RS"):
    wh = Warehouse(code=code, name="Тест склад разбраковки")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(barcode, name="Товар разбраковки"):
    item = Nomenclature(sku=barcode, barcode=barcode, name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_doc(warehouse, supplier="ИП Тестов", from_invoice=True, number="RS-0001"):
    doc = ReceivingDocument(
        number=number,
        warehouse_id=warehouse.id,
        supplier=supplier,
        invoice_file_name="накладная.xlsx" if from_invoice else None,
    )
    db.session.add(doc)
    db.session.commit()
    return doc


def _make_staff_user():
    user = User(username="staffer-rs", full_name="Складской", role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    return user


def _login_as(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_send_to_recount_and_sorting_transitions_status(db, client_logged_in):
    wh = _make_warehouse("WH-RS-1")
    item = _make_item("7770000101")
    doc = _make_doc(wh, number="RS-0002")
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10))
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/send-to-recount")
    assert ReceivingDocument.query.get(doc.id).status == "recounting"

    client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting")
    assert ReceivingDocument.query.get(doc.id).status == "sorting"

    client_logged_in.post(f"/receiving/{doc.id}/complete")
    assert ReceivingDocument.query.get(doc.id).status == "completed"


def test_complete_blocked_before_sorting(db, client_logged_in):
    """Раньше "Завершить приемку" работала прямо из черновика — теперь
    сначала нужно пройти пересчет и разбраковку."""
    wh = _make_warehouse("WH-RS-2")
    item = _make_item("7770000102")
    doc = _make_doc(wh, number="RS-0003")
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=5))
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/complete")
    doc = ReceivingDocument.query.get(doc.id)
    assert doc.status == "draft"
    assert UnplacedStock.available(wh.id, item.id) == 0


def test_recounting_allows_qty_correction(db, client_logged_in):
    wh = _make_warehouse("WH-RS-3")
    item = _make_item("7770000103")
    doc = _make_doc(wh, number="RS-0004")
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10)
    db.session.add(line)
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/send-to-recount")
    client_logged_in.post(f"/receiving/{doc.id}/lines/{line.id}/update", data={"qty": "8"})

    assert ReceivingLine.query.get(line.id).qty == 8


def test_sorting_defect_creates_return_and_credits_only_good_qty(db, client_logged_in):
    wh = _make_warehouse("WH-RS-4")
    item = _make_item("7770000104")
    doc = _make_doc(wh, supplier="ИП Бракоделов", from_invoice=True, number="RS-0005")
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10)
    db.session.add(line)
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/send-to-recount")
    client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting")
    client_logged_in.post(f"/receiving/{doc.id}/lines/{line.id}/update-defect", data={"defect_qty": "3"})
    client_logged_in.post(f"/receiving/{doc.id}/complete")

    assert UnplacedStock.available(wh.id, item.id) == 7

    ret = SupplierReturn.query.filter_by(receiving_document_id=doc.id).first()
    assert ret is not None
    assert ret.qty == 3
    assert ret.nomenclature_id == item.id
    assert ret.supplier_name == "ИП Бракоделов"
    assert ret.invoice_number == "RS-0005"


def test_defect_qty_cannot_exceed_line_qty(db, client_logged_in):
    wh = _make_warehouse("WH-RS-5")
    item = _make_item("7770000105")
    doc = _make_doc(wh, number="RS-0006")
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10)
    db.session.add(line)
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/send-to-recount")
    client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting")
    client_logged_in.post(f"/receiving/{doc.id}/lines/{line.id}/update-defect", data={"defect_qty": "999"})

    assert ReceivingLine.query.get(line.id).defect_qty == 0


def test_boxed_line_skips_sorting_and_stays_in_box(db, client_logged_in):
    """Товар, упакованный в короб прямо при приемке, минует и неразмещенный
    остаток, и разбраковку — как и раньше."""
    wh = _make_warehouse("WH-RS-6")
    item = _make_item("7770000106")
    doc = _make_doc(wh, number="RS-0007")
    box = Box(box_number="BOX-RS-1", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=4))
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=4, box_id=box.id))
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/send-to-recount")
    client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting")
    client_logged_in.post(f"/receiving/{doc.id}/complete")

    assert UnplacedStock.available(wh.id, item.id) == 0
    assert BoxItem.query.filter_by(box_id=box.id, nomenclature_id=item.id).first().qty == 4
    assert SupplierReturn.query.filter_by(receiving_document_id=doc.id).count() == 0


def test_non_admin_cannot_flag_defect_without_invoice_import(db, client):
    wh = _make_warehouse("WH-RS-7")
    item = _make_item("7770000107")
    doc = _make_doc(wh, from_invoice=False, number="RS-0008")
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10)
    db.session.add(line)
    db.session.commit()
    staff = _make_staff_user()
    _login_as(client, staff)

    client.post(f"/receiving/{doc.id}/send-to-recount")
    client.post(f"/receiving/{doc.id}/send-to-sorting")
    client.post(f"/receiving/{doc.id}/lines/{line.id}/update-defect", data={"defect_qty": "2"})

    assert ReceivingLine.query.get(line.id).defect_qty == 0


def test_admin_can_flag_defect_without_invoice_import_but_return_has_no_invoice_number(db, client_logged_in):
    """Админ может выделить брак даже для вручную созданной приемки — но
    возврат создается без invoice_number, чтобы автосинхронизация с 1С его
    не подхватила (сопоставлять там нечего — номер не настоящий номер
    накладной)."""
    wh = _make_warehouse("WH-RS-8")
    item = _make_item("7770000108")
    doc = _make_doc(wh, from_invoice=False, number="RS-0009")
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10)
    db.session.add(line)
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/send-to-recount")
    client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting")
    client_logged_in.post(f"/receiving/{doc.id}/lines/{line.id}/update-defect", data={"defect_qty": "2"})
    client_logged_in.post(f"/receiving/{doc.id}/complete")

    ret = SupplierReturn.query.filter_by(receiving_document_id=doc.id).first()
    assert ret is not None
    assert ret.qty == 2
    assert ret.invoice_number is None
