"""Сводный экспорт перемещений (/movement/export-summary.xlsx) — одна
строка на документ целиком с количеством коробов и суммарным количеством
товара, в отличие от export_all (там строка на каждый товар в каждом
коробе)."""

import io
from datetime import datetime

import openpyxl

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    MovementReceiptDiscrepancy,
    Nomenclature,
    Warehouse,
)


def _make_item(barcode, name="Товар для сводки"):
    item = Nomenclature(sku=barcode, barcode=barcode, name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_movement_with_boxes(
    number,
    box_specs,
    sender_code="WH-MSUM-A",
    receiver_code="WH-MSUM-B",
    receiver_marketplace=None,
    **doc_kwargs,
):
    """box_specs — список (qty,) на короб, по одному товару в каждом."""
    sender = Warehouse.query.filter_by(code=sender_code).first() or Warehouse(code=sender_code, name="Отправитель")
    receiver = Warehouse.query.filter_by(code=receiver_code).first() or Warehouse(code=receiver_code, name="Получатель")
    receiver.marketplace = receiver_marketplace
    if sender.id is None:
        db.session.add(sender)
    if receiver.id is None:
        db.session.add(receiver)
    db.session.commit()

    doc = MovementDocument(
        number=number, from_warehouse_id=sender.id, to_warehouse_id=receiver.id, status="completed", **doc_kwargs
    )
    db.session.add(doc)
    db.session.commit()

    for i, qty in enumerate(box_specs):
        item = _make_item(f"777{number}{i}")
        box = Box(box_number=f"BOX-{number}-{i}", warehouse_id=sender.id, status="open")
        db.session.add(box)
        db.session.commit()
        db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
        db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
    db.session.commit()
    return doc


def _read_xlsx_rows(data):
    wb = openpyxl.load_workbook(io.BytesIO(data))
    ws = wb.active
    return [row for row in ws.iter_rows(min_row=2, values_only=True) if row[0] is not None]


def test_summary_export_has_one_row_per_document_with_box_and_item_counts(db, client_logged_in):
    doc = _make_movement_with_boxes("MSUM-1", [3, 5, 2])

    resp = client_logged_in.get("/movement/export-summary.xlsx")
    rows = _read_xlsx_rows(resp.data)

    row = next(r for r in rows if r[0] == doc.number)
    assert row[10] == 3  # кол-во коробов
    assert row[11] == 10  # суммарное кол-во товара в коробах (3+5+2)
    assert row[12] == 0  # без заявки документ еще не входит в факт плана


def test_summary_export_headers(db, client_logged_in):
    _make_movement_with_boxes("MSUM-2", [1])
    resp = client_logged_in.get("/movement/export-summary.xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(resp.data))
    ws = wb.active
    header = [c.value for c in ws[1]]
    assert header == [
        "Номер документа",
        "Дата",
        "Статус",
        "Склад-источник",
        "Маркетплейс",
        "Склад-назначение",
        "№ заявки МП",
        "Заявка на МП создана",
        "Дата отправки",
        "Принято на складе",
        "Кол-во коробов",
        "Кол-во в коробах",
        "Кол-во отгружено",
    ]


def test_summary_export_includes_warehouses_marketplace_and_request_number(db, client_logged_in):
    doc = _make_movement_with_boxes(
        "MSUM-3", [4], receiver_marketplace="wb", marketplace_request_number="REQ-99"
    )

    resp = client_logged_in.get("/movement/export-summary.xlsx")
    rows = _read_xlsx_rows(resp.data)
    row = next(r for r in rows if r[0] == doc.number)

    assert row[3] == doc.from_warehouse.name
    assert row[4] == "ВБ"
    assert row[5] == doc.to_warehouse.name
    assert row[6] == "REQ-99"


def test_summary_export_marks_ozon_destination(db, client_logged_in):
    doc = _make_movement_with_boxes("MSUM-4", [2], receiver_code="WH-MSUM-OZON", receiver_marketplace="ozon")

    resp = client_logged_in.get("/movement/export-summary.xlsx")
    rows = _read_xlsx_rows(resp.data)
    row = next(r for r in rows if r[0] == doc.number)

    assert row[4] == "ОЗОН"


def test_summary_export_separates_boxed_and_actually_received_qty(db, client_logged_in):
    now = datetime.utcnow()
    doc = _make_movement_with_boxes(
        "MSUM-5",
        [10],
        receiver_code="WH-MSUM-ACTUAL",
        receiver_marketplace="wb",
        completed_at=now,
        marketplace_request_number="REQ-MSUM-5",
        marketplace_request_created_at=now,
        shipped_at=now,
        received_at=now,
    )
    box_item = doc.lines.first().box.items.first()
    db.session.add(
        MovementReceiptDiscrepancy(
            document_id=doc.id,
            nomenclature_id=box_item.nomenclature_id,
            expected_qty=10,
            received_qty=7,
        )
    )
    # Как при настоящей приемке: недовоз списан из короба.
    box_item.qty = 7
    db.session.commit()

    resp = client_logged_in.get("/movement/export-summary.xlsx")
    row = next(r for r in _read_xlsx_rows(resp.data) if r[0] == doc.number)

    assert row[7] == "Да"
    assert row[8]
    assert row[9]
    assert row[11] == 10
    assert row[12] == 7


def test_summary_export_excludes_no_documents_and_response_is_xlsx(db, client_logged_in):
    resp = client_logged_in.get("/movement/export-summary.xlsx")
    assert resp.status_code == 200
    assert resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_summary_export_has_daily_shipments_sheet(db, client_logged_in):
    """Вторая страница файла — отгрузки по дням: суммарно коробов и товара
    за каждый день отгрузки (shipped_at), документы без отметки об отгрузке
    в эту страницу не попадают (см. чат)."""
    day1 = datetime(2026, 9, 1, 10, 0)
    day2 = datetime(2026, 9, 2, 11, 0)
    _make_movement_with_boxes("MSUM-D1", [3, 2], receiver_code="WH-MSUM-D1", shipped_at=day1)
    _make_movement_with_boxes("MSUM-D2", [5], receiver_code="WH-MSUM-D2", shipped_at=day1)
    _make_movement_with_boxes("MSUM-D3", [4], receiver_code="WH-MSUM-D3", shipped_at=day2)
    _make_movement_with_boxes("MSUM-D4-NOSHIP", [10], receiver_code="WH-MSUM-D4")

    resp = client_logged_in.get("/movement/export-summary.xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(resp.data))

    assert wb.sheetnames == [
        "Перемещения (сводно)", "Отгрузки по дням", "Куда и когда (короба)", "Отгружено по видам",
        "Отгружено по видам (откуда)", "Отгружено по видам (куда)",
    ]
    ws2 = wb["Отгрузки по дням"]
    header = [c.value for c in ws2[1]]
    assert header == ["Дата отгрузки", "Кол-во коробов", "Кол-во товара, шт"]

    rows = {row[0]: (row[1], row[2]) for row in ws2.iter_rows(min_row=2, values_only=True) if row[0]}
    assert rows["2026-09-01"] == (3, 10)  # 2 короба + 1 короб = 3; (3+2)+5 = 10 шт
    assert rows["2026-09-02"] == (1, 4)
    assert len(rows) == 2  # неотгруженный документ (MSUM-D4) не попал


def test_summary_export_has_destination_pivot_sheet(db, client_logged_in):
    """Третья страница файла — сводная таблица "куда и когда поехало":
    строки это дни отгрузки, колонки — склады назначения, значения — кол-во
    коробов (см. чат: "сколько коробов куда и когда поехало"). Документы без
    отметки об отгрузке в эту страницу не попадают, как и в "Отгрузки по
    дням" выше."""
    day1 = datetime(2026, 9, 1, 10, 0)
    day2 = datetime(2026, 9, 2, 11, 0)
    _make_movement_with_boxes("MSUM-P1", [3, 2], receiver_code="WH-MSUM-P-MSK", shipped_at=day1)
    _make_movement_with_boxes("MSUM-P2", [5], receiver_code="WH-MSUM-P-KZN", shipped_at=day1)
    _make_movement_with_boxes("MSUM-P3", [4], receiver_code="WH-MSUM-P-MSK", shipped_at=day2)
    _make_movement_with_boxes("MSUM-P4-NOSHIP", [10], receiver_code="WH-MSUM-P-MSK")

    msk = Warehouse.query.filter_by(code="WH-MSUM-P-MSK").first()
    msk.name = "Москва"
    kzn = Warehouse.query.filter_by(code="WH-MSUM-P-KZN").first()
    kzn.name = "Казань"
    db.session.commit()

    resp = client_logged_in.get("/movement/export-summary.xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(resp.data))
    ws3 = wb["Куда и когда (короба)"]

    header = [c.value for c in ws3[1]]
    assert header == ["Дата отгрузки", "Казань", "Москва", "Итого"]

    rows = {row[0]: row[1:] for row in ws3.iter_rows(min_row=2, values_only=True) if row[0]}
    assert rows["2026-09-01"] == (1, 2, 3)  # 1 короб в Казань, 2 короба (MSUM-P1) в Москву
    assert rows["2026-09-02"] == (0, 1, 1)
    assert rows["Итого"] == (1, 3, 4)


def test_summary_export_date_range_filters_by_shipped_at(db, client_logged_in):
    early = datetime(2026, 8, 1, 10, 0)
    inside = datetime(2026, 9, 10, 10, 0)
    late = datetime(2026, 10, 1, 10, 0)
    doc_early = _make_movement_with_boxes("MSUM-R1", [1], receiver_code="WH-MSUM-R1", shipped_at=early)
    doc_inside = _make_movement_with_boxes("MSUM-R2", [1], receiver_code="WH-MSUM-R2", shipped_at=inside)
    doc_late = _make_movement_with_boxes("MSUM-R3", [1], receiver_code="WH-MSUM-R3", shipped_at=late)

    resp = client_logged_in.get("/movement/export-summary.xlsx?date_from=2026-09-01&date_to=2026-09-30")
    rows = _read_xlsx_rows(resp.data)
    numbers = {r[0] for r in rows}

    assert doc_inside.number in numbers
    assert doc_early.number not in numbers
    assert doc_late.number not in numbers


def test_summary_export_date_to_is_inclusive_of_whole_day(db, client_logged_in):
    end_of_day = datetime(2026, 9, 30, 23, 59)
    doc = _make_movement_with_boxes("MSUM-R4", [1], receiver_code="WH-MSUM-R4", shipped_at=end_of_day)

    resp = client_logged_in.get("/movement/export-summary.xlsx?date_from=2026-09-01&date_to=2026-09-30")
    numbers = {r[0] for r in _read_xlsx_rows(resp.data)}

    assert doc.number in numbers


def test_summary_export_without_dates_includes_everything_like_before(db, client_logged_in):
    doc_no_ship = _make_movement_with_boxes("MSUM-R5", [1], receiver_code="WH-MSUM-R5")
    doc_shipped = _make_movement_with_boxes(
        "MSUM-R6", [1], receiver_code="WH-MSUM-R6", shipped_at=datetime(2020, 1, 1)
    )

    resp = client_logged_in.get("/movement/export-summary.xlsx")
    numbers = {r[0] for r in _read_xlsx_rows(resp.data)}

    assert doc_no_ship.number in numbers
    assert doc_shipped.number in numbers


def test_list_page_has_summary_export_date_range_fields(db, client_logged_in):
    html = client_logged_in.get("/movement/").get_data(as_text=True)
    assert 'name="date_from"' in html
    assert 'name="date_to"' in html


def test_summary_export_kolvo_v_korobah_shows_live_qty_not_stale_snapshot(db, client_logged_in):
    """Регрессия: total_sent_qty() раньше отдавал замороженный на момент
    complete() sent_qty_snapshot, из-за чего сводный экспорт расходился с
    тем, что видно в самом перемещении после правки короба (см. чат:
    "экспорт показывает 2220, строка показывает 2147"). Эти цифры сверяют с
    заявками на самом маркетплейсе, поэтому нужны актуальные данные — теперь
    total_sent_qty() всегда равен "живому" total_item_qty()."""
    sender = Warehouse(code="WH-MSUM-SNAP-A", name="Отправитель")
    receiver = Warehouse(code="WH-MSUM-SNAP-B", name="Получатель")
    db.session.add_all([sender, receiver])
    db.session.commit()
    item = _make_item("777MSUM-SNAP0")
    doc = MovementDocument(
        number="MSUM-SNAP", from_warehouse_id=sender.id, to_warehouse_id=receiver.id, status="draft"
    )
    db.session.add(doc)
    db.session.commit()
    box = Box(box_number="BOX-MSUM-SNAP-0", warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=10))
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
    db.session.commit()

    # Реальное завершение через маршрут — именно оно фиксирует sent_qty_snapshot.
    resp = client_logged_in.post(f"/movement/{doc.id}/complete", follow_redirects=True)
    assert resp.status_code == 200
    doc = MovementDocument.query.get(doc.id)
    assert doc.sent_qty_snapshot == 10

    # Короб поправили постфактум — фактическое содержимое выросло.
    box_item = doc.lines.first().box.items.first()
    box_item.qty = 73
    db.session.commit()
    assert doc.total_item_qty() == 73

    rows = _read_xlsx_rows(client_logged_in.get("/movement/export-summary.xlsx").data)
    row = next(r for r in rows if r[0] == doc.number)

    assert row[11] == 73  # "Кол-во в коробах" — актуальное количество, не старый снимок 10


def test_summary_export_has_shipped_by_category_sheet(db, client_logged_in):
    """Лист «Отгружено по видам»: количество отгруженного (транспорт забрал)
    по виду товара с разбивкой ОЗОН / ВБ; неотгруженное не считается."""
    from wms.models import ProductCategory

    hats = ProductCategory(name="Шапки-тест")
    sweaters = ProductCategory(name="Свитеры-тест")
    db.session.add_all([hats, sweaters])
    db.session.commit()

    now = datetime.utcnow()
    ozon = _make_movement_with_boxes("MSUM-C1", [10, 4], receiver_code="WH-MSUM-CO", receiver_marketplace="ozon", shipped_at=now)
    wb = _make_movement_with_boxes("MSUM-C2", [6], receiver_code="WH-MSUM-CW", receiver_marketplace="wb", shipped_at=now)
    not_shipped = _make_movement_with_boxes("MSUM-C3", [99], receiver_code="WH-MSUM-CN", receiver_marketplace="ozon")
    # виды: первый товар ozon-документа и товар wb-документа — шапки, второй товар ozon — свитеры
    items = {}
    for doc in (ozon, wb, not_shipped):
        for line in doc.lines:
            for bi in line.box.items:
                items.setdefault(doc.number, []).append(bi.nomenclature)
    items["MSUM-C1"][0].category_id = hats.id
    items["MSUM-C1"][1].category_id = sweaters.id
    items["MSUM-C2"][0].category_id = hats.id
    items["MSUM-C3"][0].category_id = hats.id
    db.session.commit()

    resp = client_logged_in.get("/movement/export-summary.xlsx")
    wb_xlsx = openpyxl.load_workbook(io.BytesIO(resp.data))
    assert "Отгружено по видам" in wb_xlsx.sheetnames
    ws = wb_xlsx["Отгружено по видам"]
    assert [c.value for c in ws[1]] == ["Вид товара", "ОЗОН, шт", "ВБ, шт", "Всего, шт"]
    rows = {r[0]: r[1:] for r in ws.iter_rows(min_row=2, values_only=True)}
    assert rows["Шапки-тест"] == (10, 6, 16)
    assert rows["Свитеры-тест"] == (4, 0, 4)
    assert rows["Итого"] == (14, 6, 20)


def test_shipped_by_category_restores_qty_after_shortage_receipt(db, client_logged_in):
    from wms.models import MovementReceiptDiscrepancy

    now = datetime.utcnow()
    doc = _make_movement_with_boxes("MSUM-C4", [8], receiver_code="WH-MSUM-C4", receiver_marketplace="ozon",
                                    shipped_at=now, received_at=now)
    item = doc.lines.first().box.items.first()
    # принято 5 из 8: короб уменьшен до 5, исходные 8 хранятся в расхождении
    item.qty = 5
    db.session.add(MovementReceiptDiscrepancy(document_id=doc.id, nomenclature_id=item.nomenclature_id,
                                              expected_qty=8, received_qty=5))
    db.session.commit()

    resp = client_logged_in.get("/movement/export-summary.xlsx")
    ws = openpyxl.load_workbook(io.BytesIO(resp.data))["Отгружено по видам"]
    rows = {r[0]: r[1:] for r in ws.iter_rows(min_row=2, values_only=True)}
    assert rows["Итого"][-1] == 8


def test_shipped_by_category_is_split_by_sender_and_destination_warehouses(db, client_logged_in):
    from wms.models import ProductCategory

    hats = ProductCategory(name="Шапки-разбивка")
    db.session.add(hats)
    db.session.commit()
    now = datetime.utcnow()
    a = _make_movement_with_boxes("MSUM-D1", [10], sender_code="WH-MSUM-S1", receiver_code="WH-MSUM-D1",
                                  receiver_marketplace="ozon", shipped_at=now)
    b = _make_movement_with_boxes("MSUM-D2", [7], sender_code="WH-MSUM-S2", receiver_code="WH-MSUM-D2",
                                  receiver_marketplace="wb", shipped_at=now)
    for doc in (a, b):
        doc.lines.first().box.items.first().nomenclature.category_id = hats.id
    from wms.models import Warehouse
    Warehouse.query.filter_by(code="WH-MSUM-S1").first().name = "Основной склад"
    Warehouse.query.filter_by(code="WH-MSUM-S2").first().name = "Склад №2"
    Warehouse.query.filter_by(code="WH-MSUM-D1").first().name = "ОЗОН: Москва"
    Warehouse.query.filter_by(code="WH-MSUM-D2").first().name = "ВБ: Коледино"
    db.session.commit()

    resp = client_logged_in.get("/movement/export-summary.xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(resp.data))

    def table(sheet):
        ws = wb[sheet]
        header = [c.value for c in ws[1]]
        return header, {r[0]: dict(zip(header[1:], r[1:])) for r in ws.iter_rows(min_row=2, values_only=True)}

    header, rows = table("Отгружено по видам (откуда)")
    assert header == ["Вид товара", "Основной склад, шт", "Склад №2, шт", "Всего, шт"]
    assert rows["Шапки-разбивка"] == {"Основной склад, шт": 10, "Склад №2, шт": 7, "Всего, шт": 17}
    header, rows = table("Отгружено по видам (куда)")
    assert header == ["Вид товара", "ВБ: Коледино, шт", "ОЗОН: Москва, шт", "Всего, шт"]
    assert rows["Шапки-разбивка"]["ОЗОН: Москва, шт"] == 10 and rows["Итого"]["Всего, шт"] == 17
