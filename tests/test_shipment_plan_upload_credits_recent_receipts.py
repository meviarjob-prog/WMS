"""Загрузка новой версии плана отгрузок (shipment_plan._apply_plan)
полностью заменяет строки старой версии (plan.lines.delete()) — вместе с
ними терялся бы и накопленный fulfilled_qty, если подтвержденная приемка
по направлению случилась ДО этой загрузки. Поэтому при загрузке дополнительно
учитывается уже принятое перемещением (received_at) в интервале действия
плана: с самой "даты распределения" и до дедлайна +PERIOD_DAYS (14) — тот
же интервал, что и в shipment_plan._pace_analysis. Приемка до начала
периода или после дедлайна к текущему плану отношения не имеет — см.
shipment_plan._received_since_by_warehouse_and_item."""

import io
from datetime import date, datetime, timedelta

import openpyxl

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, MovementLine, Nomenclature, ShipmentPlanLine, Warehouse


def _plan_file(sheet_name, city, barcode, qty):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet(sheet_name)
    ws.append(["Артикул", "Размер", "Баркод", city])
    ws.append(["ART-1", "44", barcode, qty])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _make_received_movement(sender, city, item, qty, box_number, received_at):
    box = Box(box_number=box_number, warehouse_id=sender.id, status="stored")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
    doc = MovementDocument(
        number=f"PER-{box_number}",
        from_warehouse_id=sender.id,
        to_warehouse_id=city.id,
        status="completed",
        received_at=received_at,
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
    db.session.commit()


def _upload(client, sheet_name, city, barcode, qty):
    return client.post(
        "/shipment-plan/upload",
        data={"file": (_plan_file(sheet_name, city, barcode, qty), "plan.xlsx")},
        content_type="multipart/form-data",
    )


def test_upload_credits_receipt_within_period_window_into_fulfilled_qty(db, client_logged_in):
    sender = Warehouse(code="WH-UPC1", name="Склад-отправитель")
    db.session.add(sender)
    db.session.commit()
    item = Nomenclature(sku="SKU-UPC1", barcode="7770100001", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    period_start = date.today() - timedelta(days=2)
    sheet_name = f"Распределение ОЗОН ФБС от {period_start.strftime('%d.%m')}"

    # Город еще не существует как склад WMS — создастся при загрузке
    # плана, поэтому приемку "задним числом" оформляем через уже
    # созданный склад-город с ожидаемым именем/маркетплейсом... но т.к.
    # города создает сама загрузка, короб примем на склад ПОСЛЕ первой
    # (пустой по факту) загрузки, затем перезагрузим план и проверим, что
    # повторная замена строк не стирает уже подтвержденное.
    resp = _upload(client_logged_in, sheet_name, "Город", item.barcode, 30)
    assert resp.status_code == 302

    city = Warehouse.query.filter_by(marketplace="ozon", marketplace_city="Город").first()
    assert city is not None
    line = ShipmentPlanLine.query.filter_by(warehouse_id=city.id, nomenclature_id=item.id).first()
    assert line.fulfilled_qty == 0

    # Приемка внутри интервала действия плана (дата распределения .. +14 дней).
    received_at = datetime.combine(period_start, datetime.min.time()) + timedelta(days=1)
    _make_received_movement(sender, city, item, qty=12, box_number="BOX-UPC101", received_at=received_at)

    resp = _upload(client_logged_in, sheet_name, "Город", item.barcode, 30)
    assert resp.status_code == 302

    line = ShipmentPlanLine.query.filter_by(warehouse_id=city.id, nomenclature_id=item.id).first()
    assert line.fulfilled_qty == 12


def test_upload_ignores_google_fact_and_uses_only_wms_fact(db, client_logged_in):
    item = Nomenclature(sku="SKU-UPC-G", barcode="7770100099", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    period_start = date.today() - timedelta(days=2)
    sheet_name = f"Распределение ОЗОН ФБС от {period_start.strftime('%d.%m')}"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    sheet.append(["Артикул", "Размер", "Баркод", "Город", "отгружено / в пути"])
    sheet.append(["ART-1", "44", item.barcode, 30, 25])
    stream = io.BytesIO()
    workbook.save(stream)
    stream.seek(0)

    response = client_logged_in.post(
        "/shipment-plan/upload",
        data={"file": (stream, "plan.xlsx")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    line = ShipmentPlanLine.query.filter_by(nomenclature_id=item.id).first()
    assert line.planned_qty == 30
    assert line.fulfilled_qty == 0


def test_upload_ignores_receipts_before_period_start(db, client_logged_in):
    sender = Warehouse(code="WH-UPC2", name="Склад-отправитель")
    db.session.add(sender)
    db.session.commit()
    item = Nomenclature(sku="SKU-UPC2", barcode="7770100002", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    period_start = date.today() - timedelta(days=2)
    sheet_name = f"Распределение ОЗОН ФБС от {period_start.strftime('%d.%m')}"

    _upload(client_logged_in, sheet_name, "Город2", item.barcode, 30)
    city = Warehouse.query.filter_by(marketplace="ozon", marketplace_city="Город2").first()

    # Принято до начала периода плана — не должно засчитаться, это старая,
    # не относящаяся к текущему периоду приемка.
    old_received_at = datetime.combine(period_start, datetime.min.time()) - timedelta(days=5)
    _make_received_movement(sender, city, item, qty=99, box_number="BOX-UPC201", received_at=old_received_at)

    _upload(client_logged_in, sheet_name, "Город2", item.barcode, 30)

    line = ShipmentPlanLine.query.filter_by(warehouse_id=city.id, nomenclature_id=item.id).first()
    assert line.fulfilled_qty == 0


def test_upload_counts_shipments_after_fourteen_day_deadline(db, client_logged_in):
    sender = Warehouse(code="WH-UPC3", name="Склад-отправитель")
    db.session.add(sender)
    db.session.commit()
    item = Nomenclature(sku="SKU-UPC3", barcode="7770100003", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    # У даты плана нет верхней границы: все более поздние отгрузки закрывают
    # потребность до появления листа с новой датой.
    period_start = date.today() - timedelta(days=20)
    sheet_name = f"Распределение ОЗОН ФБС от {period_start.strftime('%d.%m')}"

    _upload(client_logged_in, sheet_name, "Город3", item.barcode, 30)
    city = Warehouse.query.filter_by(marketplace="ozon", marketplace_city="Город3").first()

    # Принято спустя 20 дней после даты плана — все равно относится к нему.
    late_received_at = datetime.combine(period_start, datetime.min.time()) + timedelta(days=20)
    _make_received_movement(sender, city, item, qty=77, box_number="BOX-UPC301", received_at=late_received_at)

    _upload(client_logged_in, sheet_name, "Город3", item.barcode, 30)

    line = ShipmentPlanLine.query.filter_by(warehouse_id=city.id, nomenclature_id=item.id).first()
    assert line.fulfilled_qty == 77


def test_repeated_upload_reuses_canonical_city_warehouse(db, client_logged_in):
    item = Nomenclature(sku="SKU-CITY-NORM", barcode="7770100098", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    sheet_name = f"Распределение ОЗОН ФБС от {date.today().strftime('%d.%m')}"

    assert _upload(client_logged_in, sheet_name, "ОЗОН: МОСКВА", item.barcode, 30).status_code == 302
    first = Warehouse.query.filter_by(marketplace="ozon").one()
    assert first.name == "Москва"
    assert first.marketplace_city == "Москва"

    assert _upload(client_logged_in, sheet_name, "  Москва  ", item.barcode, 40).status_code == 302
    warehouses = Warehouse.query.filter_by(marketplace="ozon").all()
    assert len(warehouses) == 1
    assert warehouses[0].id == first.id
    line = ShipmentPlanLine.query.filter_by(warehouse_id=first.id, barcode=item.barcode).one()
    assert line.planned_qty == 40
