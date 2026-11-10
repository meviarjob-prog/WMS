"""Приемка из накладной: если товар из строки накладной уже упаковали в
короб прямо в этой приемке, при завершении он не должен зачисляться еще и
в неразмещенный остаток — иначе остаток задваивается (в коробах 40 и
«неразмещенных» еще 40)."""

from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, ReceivingDocument, ReceivingLine, UnplacedStock, Warehouse


def _setup(expected):
    wh = Warehouse(code="WH-DBL", name="Склад №2 (Шоссейная 167)")
    item = Nomenclature(sku="kard-sin", barcode="2056744446961", name="кардиган синий", unit="шт")
    db.session.add_all([wh, item])
    db.session.flush()
    box = Box(box_number="BOX-DBL-1", warehouse_id=wh.id)
    doc = ReceivingDocument(number="PR-DBL-1", warehouse_id=wh.id, invoice_file_name="накладная.xlsx", status="draft")
    db.session.add_all([box, doc])
    db.session.flush()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=expected, expected_qty=expected))
    db.session.commit()
    return wh, item, box, doc


def _finish(client, doc, item):
    client.post(f"/receiving/{doc.id}/send-to-recount")
    line = ReceivingLine.query.filter_by(document_id=doc.id, box_id=None, nomenclature_id=item.id).first()
    if line is not None:
        client.post(f"/receiving/{doc.id}/lines/{line.id}/confirm", json={"qty": line.qty, "confirmed": True})
    client.post(f"/receiving/{doc.id}/send-to-sorting")
    client.post(f"/receiving/{doc.id}/complete")


def test_boxed_part_of_invoice_line_not_credited_twice(db, client_logged_in):
    wh, item, box, doc = _setup(40)
    client_logged_in.post(f"/receiving/{doc.id}/boxes/{box.id}/lines/add", data={"nomenclature_id": item.id, "qty": "40"})
    _finish(client_logged_in, doc, item)
    assert ReceivingDocument.query.get(doc.id).status == "completed"
    assert BoxItem.query.filter_by(box_id=box.id).one().qty == 40
    assert UnplacedStock.available(wh.id, item.id) == 0


def test_only_unboxed_rest_of_invoice_line_goes_to_unplaced(db, client_logged_in):
    wh, item, box, doc = _setup(50)
    client_logged_in.post(f"/receiving/{doc.id}/boxes/{box.id}/lines/add", data={"nomenclature_id": item.id, "qty": "40"})
    _finish(client_logged_in, doc, item)
    assert UnplacedStock.available(wh.id, item.id) == 10


def _box_receipt(wh, item, number, qty, when):
    from wms.models import Box
    box = Box(box_number=f"BOX-{number}", warehouse_id=wh.id)
    doc = ReceivingDocument(number=number, warehouse_id=wh.id, status="completed", created_at=when, completed_at=when)
    db.session.add_all([box, doc])
    db.session.flush()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=qty, box_id=box.id))
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
    return doc


def _invoice_credit(wh, item, number, qty, when):
    from wms.models import UnplacedStockLot
    doc = ReceivingDocument(number=number, warehouse_id=wh.id, invoice_file_name="н.xlsx", status="completed",
                            created_at=when, completed_at=when, supplier="ООО Пряжа")
    db.session.add(doc)
    db.session.flush()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=qty, expected_qty=qty,
                                 confirmed=True, line_completed_at=when))
    UnplacedStock.add(wh.id, item.id, qty, receiving_document=doc)
    UnplacedStockLot.query.filter_by(receiving_document_id=doc.id).one().received_at = when
    return doc


def test_boxed_before_invoice_receipt_is_explicit_double(db, client_logged_in):
    """Короба приняли (40) раньше, чем завели приемку накладной на тот же
    товар (+40 неразмещенных): остаток ушел бы в минус −40, зачисление его
    закрыло — 40 задвоено. Списание один раз."""
    from datetime import datetime
    from wms.models import AppSetting
    wh = Warehouse(code="WH-D2", name="Склад №2 (Шоссейная 167)")
    item = Nomenclature(sku="k2", barcode="2056700000099", name="кардиган ласковый синий", unit="шт")
    db.session.add_all([wh, item])
    db.session.flush()
    box_doc = _box_receipt(wh, item, "PR-BOX", 40, datetime(2026, 9, 1, 10))
    inv_doc = _invoice_credit(wh, item, "PR-INV", 40, datetime(2026, 9, 3, 10))
    db.session.commit()

    page = client_logged_in.get("/receiving/doubled").get_data(as_text=True)
    assert "PR-BOX" in page and "PR-INV" in page
    client_logged_in.post("/receiving/doubled", data={"all": "1"})
    assert UnplacedStock.available(wh.id, item.id) == 0
    assert AppSetting.query.get(f"rcv_dbl:{box_doc.id}:{inv_doc.id}:{item.id}") is not None
    assert "PR-BOX" not in client_logged_in.get("/receiving/doubled").get_data(as_text=True)
    UnplacedStock.add(wh.id, item.id, 5)
    db.session.commit()
    client_logged_in.post("/receiving/doubled", data={"all": "1"})
    assert UnplacedStock.available(wh.id, item.id) == 5


def test_box_receipt_with_stock_available_or_credit_before_is_not_double(db, client_logged_in):
    """Накладную зачислили раньше (+40), потом приняли в короб 40 — остаток
    был, в минус не ушли: задвоения нет."""
    from datetime import datetime
    wh = Warehouse(code="WH-D3", name="Склад")
    item = Nomenclature(sku="k3", barcode="2056700000098", name="шапка", unit="шт")
    db.session.add_all([wh, item])
    db.session.flush()
    _invoice_credit(wh, item, "PR-INV3", 40, datetime(2026, 9, 1, 10))
    _box_receipt(wh, item, "PR-BOX3", 40, datetime(2026, 9, 2, 10))
    db.session.commit()
    assert "Явных задвоений не найдено" in client_logged_in.get("/receiving/doubled").get_data(as_text=True)


def test_doubled_report_ignores_receipts_completed_after_fix(db, client_logged_in):
    wh, item, box, doc = _setup(50)
    client_logged_in.post(f"/receiving/{doc.id}/boxes/{box.id}/lines/add", data={"nomenclature_id": item.id, "qty": "40"})
    _finish(client_logged_in, doc, item)
    assert "Явных задвоений не найдено" in client_logged_in.get("/receiving/doubled").get_data(as_text=True)


def test_doubled_report_survives_old_rows_without_dates(db, client_logged_in):
    """Партии без даты и удаленный товар не роняют отчет."""
    from datetime import datetime
    from wms.models import UnplacedStockLot
    wh = Warehouse(code="WH-D4", name="Склад")
    item = Nomenclature(sku="k4", barcode="2056700000097", name="шарф", unit="шт")
    db.session.add_all([wh, item])
    db.session.flush()
    box_doc = _box_receipt(wh, item, "PR-BOX4", 10, datetime(2026, 9, 1, 10))
    inv_doc = _invoice_credit(wh, item, "PR-INV4", 10, datetime(2026, 9, 2, 10))
    db.session.commit()
    try:
        db.session.execute(db.text("UPDATE unplaced_stock_lots SET received_at = NULL"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    db.session.expire_all()
    assert client_logged_in.get("/receiving/doubled").status_code == 200
