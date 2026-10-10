"""«В пути» в плане — перемещения, которые транспорт забрал с даты листа."""

import io
import re

from openpyxl import load_workbook

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    Nomenclature,
    ReceivingDocument,
    ReceivingLine,
    ShipmentPlan,
    ShipmentPlanLine,
    Warehouse,
)


def _setup(planned_qty=30):
    sender = Warehouse(code="WH-D1", name="Склад-отправитель")
    city = Warehouse(code="WH-D2", name="ОЗОН: Город", marketplace="ozon", marketplace_city="Город")
    db.session.add_all([sender, city])
    db.session.commit()

    item = Nomenclature(sku="SKU-D1", barcode="7770000001", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    plan = ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    db.session.commit()
    line = ShipmentPlanLine(
        plan_id=plan.id,
        warehouse_id=city.id,
        nomenclature_id=item.id,
        barcode=item.barcode,
        article="ART-1",
        planned_qty=planned_qty,
        fulfilled_qty=0,
    )
    db.session.add(line)
    db.session.commit()
    return sender, city, item


def _ship_box(sender, city, item, qty, box_number, client, mark_request=True):
    box = Box(box_number=box_number, warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
    db.session.commit()

    doc = MovementDocument(number=f"PER-{box_number}", from_warehouse_id=sender.id, to_warehouse_id=city.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=box.warehouse_id, from_cell_id=box.cell_id)
    )
    db.session.commit()

    client.post(f"/movement/{doc.id}/complete")
    if mark_request:
        doc.marketplace_request_number = f"REQ-{box_number}"
        db.session.commit()
        client.post(f"/movement/{doc.id}/mark-marketplace-request")
        client.post(f"/movement/{doc.id}/mark-shipped")
    return doc


def test_dashboard_top_summary_shows_in_transit(db, client_logged_in):
    sender, city, item = _setup(planned_qty=30)
    doc = _ship_box(sender, city, item, qty=10, box_number="BOX-000001", client=client_logged_in)

    doc = MovementDocument.query.get(doc.id)
    assert doc.status == "completed"
    assert doc.received_at is None

    resp = client_logged_in.get("/shipment-plan/")
    html = resp.get_data(as_text=True)
    idx = html.find("<tbody>", html.find("Город"))
    assert "10" in html[idx : idx + 700]


def test_city_in_transit_includes_shipped_sku_missing_from_current_plan(
    db, client_logged_in
):
    """Городская сумма — все отгрузки, а не только совпавшие строки плана."""
    from datetime import date, timedelta

    sender, city, planned_item = _setup(planned_qty=30)
    plan = ShipmentPlan.query.filter_by(marketplace="ozon").first()
    plan.period_start = date.today() - timedelta(days=1)
    unplanned_item = Nomenclature(
        sku="SKU-NOT-IN-PLAN",
        barcode="7770000999",
        name="Товар вне актуального плана",
        unit="шт",
    )
    db.session.add(unplanned_item)
    db.session.commit()

    _ship_box(
        sender,
        city,
        planned_item,
        qty=10,
        box_number="BOX-PLANNED-CITY-TOTAL",
        client=client_logged_in,
    )
    _ship_box(
        sender,
        city,
        unplanned_item,
        qty=824,
        box_number="BOX-UNPLANNED-CITY-TOTAL",
        client=client_logged_in,
        mark_request=True,
    )

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    city_idx = html.find("<td>Город</td>")
    city_snippet = html[city_idx : city_idx + 1500]
    assert ">834<" in city_snippet
    top_idx = html.find("в пути")
    assert "<b>834</b>" in html[top_idx : top_idx + 100]


def test_picking_list_shows_plan_and_in_transit_separately(db, client_logged_in):
    """В таблице остается остаток плана, товар в пути показан в скобках.
    Защита от лишней отправки применяется отдельно в маршрутизации."""
    sender, city, item = _setup(planned_qty=30)
    _ship_box(sender, city, item, qty=10, box_number="BOX-000002", client=client_logged_in)

    resp = client_logged_in.get("/shipment-plan/")
    html = resp.get_data(as_text=True)
    idx = html.find("ART-1")
    snippet = html[idx : idx + 3000]

    assert ">30<" in snippet
    assert "(10)" in snippet


def test_completed_movement_without_transport_pickup_is_not_in_transit(db, client_logged_in):
    sender, city, item = _setup(planned_qty=30)
    _ship_box(
        sender,
        city,
        item,
        qty=10,
        box_number="BOX-NO-REQUEST",
        client=client_logged_in,
        mark_request=False,
    )

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    idx = html.find("ART-1")
    snippet = html[idx : idx + 3000]
    assert ">30<" in snippet
    assert "(10)" not in snippet


def test_top_summary_shows_in_transit_per_marketplace_and_total(db, client_logged_in):
    sender, city, item = _setup(planned_qty=30)
    _ship_box(sender, city, item, qty=10, box_number="BOX-SUM-1", client=client_logged_in)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    assert "Сводка" in html
    idx = html.find("В пути")
    snippet = html[idx : idx + 600]
    assert "ОЗОН" in snippet
    assert ">10<" in snippet
    assert "Итого" in snippet


def test_top_summary_shows_total_stock(db, client_logged_in):
    from wms.models import UnplacedStock

    sender, city, item = _setup(planned_qty=30)
    UnplacedStock.add(sender.id, item.id, 25)
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    idx = html.find("На складе:")
    assert "25" in html[idx : idx + 100]


def test_top_summary_shows_total_production_since_period_start(db, client_logged_in):
    from datetime import date, timedelta

    from wms.models import ProductionRecord, User

    sender, city, item = _setup(planned_qty=30)
    plan = ShipmentPlan.query.filter_by(marketplace="ozon").first()
    plan.period_start = date.today() - timedelta(days=3)
    db.session.commit()

    worker = User(username="prod-worker", role="production")
    worker.set_password("x")
    db.session.add(worker)
    db.session.commit()

    # В периоде плана — должно засчитаться.
    db.session.add(
        ProductionRecord(user_id=worker.id, nomenclature_id=item.id, work_date=date.today())
    )
    # До начала периода — не должно засчитаться.
    db.session.add(
        ProductionRecord(
            user_id=worker.id, nomenclature_id=item.id, work_date=date.today() - timedelta(days=10)
        )
    )
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    idx = html.find("На производстве")
    assert ">1<" in html[idx : idx + 200]


def test_marketplace_header_uses_only_completed_movement_fact(db, client_logged_in):
    from datetime import date, timedelta

    sender, city, item = _setup(planned_qty=30)
    plan = ShipmentPlan.query.filter_by(marketplace="ozon").first()
    plan.period_start = date.today() - timedelta(days=3)
    line = ShipmentPlanLine.query.first()
    line.fulfilled_qty = 5
    db.session.commit()
    _ship_box(sender, city, item, qty=10, box_number="BOX-SUM-2", client=client_logged_in)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    # Сохраненный старый счетчик 5 не участвует: факт WMS — 10 отправлено.
    idx = html.find("в пути")
    snippet = html[idx : idx + 200]
    assert "<b>10</b>" in snippet
    assert "Пока нет отгрузок" not in html
    assert "Выполнено" not in html

    city_idx = html.find("<td>Город</td>")
    city_snippet = html[city_idx : city_idx + 1500]
    assert "Все завершенные перемещения с 00:01 даты листа" in html
    assert ">10<" in city_snippet


def test_dashboard_keeps_sent_qty_after_marketplace_receives_less(
    db, client_logged_in
):
    sender, city, item = _setup(planned_qty=30)
    doc = _ship_box(
        sender,
        city,
        item,
        qty=6,
        box_number="BOX-RECEIVED-FACT",
        client=client_logged_in,
    )
    client_logged_in.post(
        f"/movement/{doc.id}/receive",
        data={f"qty_{item.id}": "4"},
    )
    line = ShipmentPlanLine.query.first()
    line.fulfilled_qty = 0
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    idx = html.find("в пути")
    # Отправлено 6; недовоз 2 ведется отдельно и не уменьшает отгрузку WMS.
    assert "<b>6</b>" in html[idx : idx + 200]


def test_excel_export_matches_dashboard_table_and_keeps_transit_separate(db, client_logged_in):
    sender, city, item = _setup(planned_qty=30)
    line = ShipmentPlanLine.query.first()
    line.fulfilled_qty = 5
    db.session.commit()
    _ship_box(sender, city, item, qty=10, box_number="BOX-XLSX-1", client=client_logged_in)

    response = client_logged_in.get("/shipment-plan/export.xlsx")

    assert response.status_code == 200
    workbook = load_workbook(io.BytesIO(response.data), data_only=True)
    sheet = workbook["План отгрузок"]
    assert "A1:A2" in {str(cell_range) for cell_range in sheet.merged_cells.ranges}
    assert sheet["A1"].value == "Артикул"
    assert sheet["E1"].value == "Общий план"
    assert sheet["F1"].value == "Не хватает по плану"
    assert sheet["G1"].value == "На разбраковке"
    assert sheet["J1"].value == "ОЗОН"
    assert sheet["J2"].value == "Город"
    assert sheet["K1"].value == "Приоритет"
    assert sheet["L1"].value == "Новинка"
    assert sheet["M1"].value == "Комментарий закупщиков"
    assert sheet["A3"].value == "Итого (1 поз.)"
    assert sheet["E3"].value == 30
    assert sheet["I3"].value == 10
    assert sheet["J3"].value == 30
    assert sheet["A4"].value == "ART-1"
    assert sheet["J4"].value == "30 (10)"


def test_picking_list_shows_total_planned_and_shortfall_columns(db, client_logged_in):
    """«Общий план» и «Не хватает по плану» — построчно по товару, сумма по
    всем городам обоих маркетплейсов. «Не хватает» = план минус все, что уже
    в пути, готово к отгрузке и на разбраковке (не меньше нуля), а не просто
    остаток плана без учета того, что уже есть на руках."""
    sender, city, item = _setup(planned_qty=30)
    _ship_box(sender, city, item, qty=10, box_number="BOX-TOTALS-1", client=client_logged_in)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    assert "Общий план" in html
    assert "Не хватает по плану" in html
    idx = html.find("ART-1")
    snippet = html[idx : idx + 3000]
    assert ">30<" in snippet  # Общий план
    assert ">20<" in snippet  # Не хватает: 30 - 10 в пути


def test_shortfall_column_floors_at_zero_when_transit_covers_plan(db, client_logged_in):
    sender, city, item = _setup(planned_qty=10)
    _ship_box(sender, city, item, qty=10, box_number="BOX-TOTALS-2", client=client_logged_in)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    idx = html.find("ART-1")
    snippet = html[idx : idx + 3000]
    assert ">10<" in snippet  # Общий план и «в пути» совпадают
    assert "text-muted\">0<" in snippet  # Не хватает: max(10 - 10, 0) = 0


def test_shortfall_column_also_subtracts_ready_to_ship_and_unplaced(db, client_logged_in):
    """Товар, который уже упакован в короб на складе-отправителе (готово к
    отгрузке) или принят, но еще не упакован (на разбраковке), тоже
    закрывает потребность плана — не только то, что уже уехало."""
    from wms.models import Box, BoxItem, UnplacedStock

    sender, city, item = _setup(planned_qty=30)
    UnplacedStock.add(sender.id, item.id, 5)  # на разбраковке
    box = Box(box_number="BOX-TOTALS-READY", warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=7))  # готово к отгрузке
    db.session.commit()
    _ship_box(sender, city, item, qty=10, box_number="BOX-TOTALS-3", client=client_logged_in)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    idx = html.find("ART-1")
    snippet = html[idx : idx + 3000]
    # Не хватает: 30 (план) - 10 (в пути) - 7 (готово к отгрузке) - 5 (на разбраковке) = 8
    assert ">8<" in snippet


def test_picking_list_has_no_comment_column(db, client_logged_in):
    """Колонка и форма ввода "Комментарий закупщиков" убраны из таблицы
    (см. чат) — сам buyer_comment в модели остается, комментарий просто
    больше не редактируется и не показывается в этом месте интерфейса."""
    sender, city, item = _setup(planned_qty=30)
    line = ShipmentPlanLine.query.first()
    line.buyer_comment = "Поставка задерживается на неделю"
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    assert "Комментарий закупщиков" not in html
    assert "Поставка задерживается на неделю" not in html


def test_update_comment_endpoint_saves_buyer_comment(db, client_logged_in):
    sender, city, item = _setup(planned_qty=30)

    response = client_logged_in.post(
        f"/shipment-plan/comment/{item.barcode}",
        data={"comment": "Уточнить у поставщика"},
    )

    assert response.status_code == 302
    line = ShipmentPlanLine.query.first()
    assert line.buyer_comment == "Уточнить у поставщика"


def test_update_comment_endpoint_clears_comment_on_empty_input(db, client_logged_in):
    sender, city, item = _setup(planned_qty=30)
    line = ShipmentPlanLine.query.first()
    line.buyer_comment = "Старый комментарий"
    db.session.commit()

    client_logged_in.post(f"/shipment-plan/comment/{item.barcode}", data={"comment": "   "})

    line = ShipmentPlanLine.query.first()
    assert line.buyer_comment is None


def test_update_comment_endpoint_updates_all_lines_sharing_barcode(db, client_logged_in):
    """Один штрихкод может встречаться в нескольких строках плана (разные
    города и площадки) — комментарий один на товар, должен обновиться сразу
    во всех, иначе "потеряется" при показе другого направления того же
    товара."""
    sender, city, item = _setup(planned_qty=30)
    other_city = Warehouse(
        code="WH-D3", name="ВБ: Город2", marketplace="wb", marketplace_city="Город2"
    )
    db.session.add(other_city)
    db.session.commit()
    wb_plan = ShipmentPlan(marketplace="wb")
    db.session.add(wb_plan)
    db.session.commit()
    db.session.add(
        ShipmentPlanLine(
            plan_id=wb_plan.id,
            warehouse_id=other_city.id,
            nomenclature_id=item.id,
            barcode=item.barcode,
            article="ART-1",
            planned_qty=10,
        )
    )
    db.session.commit()

    client_logged_in.post(
        f"/shipment-plan/comment/{item.barcode}", data={"comment": "Общий комментарий"}
    )

    comments = {
        line.buyer_comment
        for line in ShipmentPlanLine.query.filter_by(barcode=item.barcode).all()
    }
    assert comments == {"Общий комментарий"}


def test_update_comment_endpoint_unknown_barcode_flashes_error(db, client_logged_in):
    response = client_logged_in.post(
        "/shipment-plan/comment/0000000000000", data={"comment": "test"}, follow_redirects=True
    )

    assert "не найден" in response.get_data(as_text=True)


def test_picking_list_has_totals_row_summing_columns(db, client_logged_in):
    """Строка "Итого" сразу под заголовками — просто сумма по столбцам
    (На разбраковке/Готово к отгрузке/В пути и по каждому городу)."""
    from wms.models import UnplacedStock

    sender, city, item = _setup(planned_qty=30)
    UnplacedStock.add(sender.id, item.id, 8)
    db.session.commit()
    _ship_box(sender, city, item, qty=10, box_number="BOX-TOT-1", client=client_logged_in)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    idx = html.find("Итого (")
    assert idx != -1
    snippet = html[idx : idx + 800]
    assert "1 поз." in snippet
    assert ">8<" in snippet  # На разбраковке
    assert ">30<" in snippet  # Остаток плана; 10 в пути показаны отдельно
    assert ">10<" in snippet  # В пути


def test_picking_list_has_v_puti_column(db, client_logged_in):
    sender, city, item = _setup(planned_qty=30)
    _ship_box(sender, city, item, qty=10, box_number="BOX-TOT-2", client=client_logged_in)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    idx = html.find("В пути")
    assert idx != -1
    idx2 = html.find("ART-1")
    row_snippet = html[idx2 : idx2 + 1200]
    assert "10" in row_snippet


def test_totals_row_not_hidden_by_search_filter(db, client_logged_in):
    sender, city, item = _setup(planned_qty=30)
    _ship_box(sender, city, item, qty=10, box_number="BOX-TOT-3", client=client_logged_in)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    assert "picking-totals-row" in html


def test_picking_list_keeps_item_when_plan_is_fully_in_transit(db, client_logged_in):
    """Даже когда весь план уже едет, строка остается видимой как
    «план (в пути)», но маршрутизация больше не предлагает этот город."""
    sender, city, item = _setup(planned_qty=10)
    _ship_box(sender, city, item, qty=10, box_number="BOX-000003", client=client_logged_in)

    resp = client_logged_in.get("/shipment-plan/")
    html = resp.get_data(as_text=True)
    assert "ART-1" in html
    idx = html.find("ART-1")
    snippet = html[idx : idx + 3000]
    assert ">10<" in snippet
    assert "(10)" in snippet


def test_picking_list_shows_receiving_on_recount_and_sorting_as_unplaced(db, client_logged_in):
    """С момента "Отправить на пересчет" и до завершения приемки товар еще
    не попал в UnplacedStock (см. receiving.complete), но физически уже
    принят на складе — должен считаться "на разбраковке" наравне с обычным
    неразмещенным остатком, а не пропадать из плана как будто его нет."""
    sender, city, item = _setup(planned_qty=30)
    # Пересчет/разбраковка доступны только приемкам из накладной (см.
    # ReceivingDocument.is_from_invoice_import) — иначе send-to-recount
    # откажет.
    doc = ReceivingDocument(
        number="REC-PLAN-1",
        warehouse_id=sender.id,
        supplier="ИП Тестов",
        invoice_file_name="накладная.xlsx",
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=12))
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/send-to-recount")

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    idx = html.find("ART-1")
    snippet = html[idx : idx + 3000]
    assert ">12<" in snippet  # На разбраковке

    client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting")

    line = ReceivingLine.query.filter_by(document_id=doc.id).first()
    client_logged_in.post(f"/receiving/{doc.id}/lines/{line.id}/update-defect", data={"defect_qty": "2"})

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    idx = html.find("ART-1")
    snippet = html[idx : idx + 3000]
    assert ">10<" in snippet  # 12 - 2 брака = 10 годного "на разбраковке"


def test_picking_list_ignores_invoice_receiving_still_in_draft(db, client_logged_in):
    """Заявленное в накладной количество еще не является поступившим:
    черновик не должен попадать в колонку «На разбраковку» или остаток."""
    sender, city, item = _setup(planned_qty=30)
    doc = ReceivingDocument(
        number="REC-PLAN-2",
        warehouse_id=sender.id,
        supplier="ИП Тестов",
        invoice_file_name="накладная.xlsx",
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=7))
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    idx = html.find("ART-1")
    snippet = html[idx : idx + 3000]
    assert ">7<" not in snippet


def test_picking_list_counts_only_confirmed_invoice_lines_on_recount(db, client_logged_in):
    """На пересчете показываем фактически подтвержденное количество, а не
    все заявленные поставщиком позиции."""
    sender, city, item = _setup(planned_qty=30)
    other = Nomenclature(sku="SKU-D2", barcode="7770000002", name="Не поступил", unit="шт")
    db.session.add(other)
    db.session.commit()
    doc = ReceivingDocument(
        number="REC-PLAN-FACT",
        warehouse_id=sender.id,
        supplier="ИП Тестов",
        invoice_file_name="накладная.xlsx",
        status="recounting",
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add_all(
        [
            ReceivingLine(
                document_id=doc.id,
                nomenclature_id=item.id,
                qty=6,
                expected_qty=10,
                confirmed=True,
            ),
            ReceivingLine(
                document_id=doc.id,
                nomenclature_id=other.id,
                qty=9,
                expected_qty=9,
                confirmed=False,
            ),
        ]
    )
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    idx = html.find("ART-1")
    snippet = html[idx : idx + 3000]
    assert ">6<" in snippet


def test_picking_list_ignores_plain_draft_receiving_without_invoice(db, client_logged_in):
    """Обычная приемка в короба (не из накладной) в draft еще может быть не
    досчитана кладовщиком — в отличие от накладной, тут нет отдельного шага
    подтверждения количества, поэтому в "на разбраковке"/остаток она не
    попадает до самого завершения приемки (см. receiving.complete)."""
    sender, city, item = _setup(planned_qty=30)
    doc = ReceivingDocument(
        number="REC-PLAN-3",
        warehouse_id=sender.id,
        supplier="ИП Тестов",
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=9))
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    idx = html.find("ART-1")
    snippet = html[idx : idx + 3000]
    assert ">9<" not in snippet


def test_ready_to_ship_split_only_for_the_two_named_sender_warehouses(db, client_logged_in):
    """Колонки "Готово к отгрузке" разбиваются только на "Основной склад" и
    "Склад №2 (Шоссейная 167)" (см. чат) — а не на любой склад-отправитель.
    Остаток на постороннем складе-отправителе (например, цех/производство)
    по-прежнему учитывается в общем "Не хватает по плану", но своей
    колонки не получает."""
    from wms.models import Box, BoxItem, Nomenclature, ShipmentPlan, ShipmentPlanLine, Warehouse

    main = Warehouse(code="WH-RTS1", name="Основной склад")
    secondary = Warehouse(code="WH-RTS2", name="Склад №2 (Шоссейная 167)")
    other = Warehouse(code="WH-RTS3", name="Цех раскроя")
    city = Warehouse(code="WH-RTS4", name="ОЗОН: Город", marketplace="ozon", marketplace_city="Город")
    db.session.add_all([main, secondary, other, city])
    db.session.commit()

    item = Nomenclature(sku="SKU-RTS1", barcode="7770000555", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    plan = ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    db.session.commit()
    db.session.add(
        ShipmentPlanLine(
            plan_id=plan.id,
            warehouse_id=city.id,
            nomenclature_id=item.id,
            barcode=item.barcode,
            article="ART-RTS",
            planned_qty=30,
        )
    )
    db.session.commit()

    for wh, box_number, qty in ((main, "BOX-RTS-MAIN", 4), (secondary, "BOX-RTS-SEC", 5), (other, "BOX-RTS-OTHER", 6)):
        box = Box(box_number=box_number, warehouse_id=wh.id, status="open")
        db.session.add(box)
        db.session.commit()
        db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
        db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    assert "Основной склад" in html
    assert "Склад №2 (Шоссейная 167)" in html
    assert "Цех раскроя" not in html

    idx = html.find("ART-RTS")
    snippet = html[idx : idx + 3000]
    assert ">4<" in snippet
    assert ">5<" in snippet


def test_picking_list_has_collapse_all_groups_toggle(db, client_logged_in):
    """Общий переключатель "Свернуть все группы" (см. чат) — форсирует
    состояние всех заголовков модель/цвет сразу, не только того, по
    которому кликнули."""
    _setup(planned_qty=30)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    assert 'data-picking-collapse-all="picking"' in html
    assert "Свернуть все группы" in html


def test_ready_to_ship_box_count_deduplicates_mixed_box_within_group(db, client_logged_in):
    """Один физический короб с двумя размерами одной модели+цвета должен
    посчитаться как ОДИН короб при сворачивании в группу "Цвет" — а не как
    два (по разу на каждый размер, см. чат: "проверь насколько правильно
    он считает кол-во коробов")."""
    from wms.models import Box, BoxItem, Nomenclature, ShipmentPlan, ShipmentPlanLine, Warehouse

    main = Warehouse(code="WH-BOXCOUNT1", name="Основной склад")
    city = Warehouse(code="WH-BOXCOUNT2", name="ОЗОН: Город", marketplace="ozon", marketplace_city="Город")
    db.session.add_all([main, city])
    db.session.commit()

    item_s = Nomenclature(sku="SKU-BC-S", barcode="7770000601", name="Товар", unit="шт", size="S")
    item_m = Nomenclature(sku="SKU-BC-M", barcode="7770000602", name="Товар", unit="шт", size="M")
    db.session.add_all([item_s, item_m])
    db.session.commit()

    plan = ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    db.session.commit()
    db.session.add_all(
        [
            ShipmentPlanLine(
                plan_id=plan.id, warehouse_id=city.id, nomenclature_id=item_s.id,
                barcode=item_s.barcode, article="Модель_цветБокс", size="S", planned_qty=10,
            ),
            ShipmentPlanLine(
                plan_id=plan.id, warehouse_id=city.id, nomenclature_id=item_m.id,
                barcode=item_m.barcode, article="Модель_цветБокс", size="M", planned_qty=10,
            ),
        ]
    )
    db.session.commit()

    # Оба размера — В ОДНОМ И ТОМ ЖЕ коробе.
    box = Box(box_number="BOX-BOXCOUNT-MIX", warehouse_id=main.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item_s.id, qty=3))
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item_m.id, qty=4))
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    color_idx = html.find("Цвет: цветБокс")
    assert color_idx != -1
    snippet = html[color_idx : color_idx + 1500]
    # Суммарно 3+4=7 штук в ОДНОМ коробе — не в двух.
    assert "(1)" in snippet
    assert "(2)" not in snippet


def test_split_article_model_color_handles_slash_separator(db):
    """Цвет в артикуле может идти и через '_' (Альма_2горла_бордо), и через
    '/' (ВзрослаяБазовая/белая, см. чат) — оба варианта должны
    группироваться как модель+цвет, а не как одна большая "модель" без
    цвета."""
    from wms.blueprints.shipment_plan import _split_article_model_color

    assert _split_article_model_color("Альма_2горла_бордо") == ("Альма_2горла", "бордо")
    assert _split_article_model_color("ВзрослаяБазовая/белая") == ("ВзрослаяБазовая", "белая")
    assert _split_article_model_color("Горшок") == ("Горшок", "")
    assert _split_article_model_color("") == ("", "")
    assert _split_article_model_color(None) == ("", "")


def test_picking_groups_merge_slash_separated_colors_under_one_model(db, client_logged_in):
    """"ВзрослаяБазовая/белая" и "ВзрослаяБазовая/голубой" — один и тот же
    товар (модель) в разных цветах, должны попасть в ОДНУ группу модели с
    двумя подгруппами цвета — не в две отдельные "модели" без цвета (см.
    скриншот в чате)."""
    from wms.models import Nomenclature, ShipmentPlan, ShipmentPlanLine, Warehouse

    city = Warehouse(code="WH-SLASH1", name="ОЗОН: Город", marketplace="ozon", marketplace_city="Город")
    db.session.add(city)
    db.session.commit()

    item_white = Nomenclature(sku="SKU-SLASH-W", barcode="7770000701", name="Товар белый", unit="шт")
    item_blue = Nomenclature(sku="SKU-SLASH-B", barcode="7770000702", name="Товар голубой", unit="шт")
    db.session.add_all([item_white, item_blue])
    db.session.commit()

    plan = ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    db.session.commit()
    db.session.add_all(
        [
            ShipmentPlanLine(
                plan_id=plan.id, warehouse_id=city.id, nomenclature_id=item_white.id,
                barcode=item_white.barcode, article="ВзрослаяБазовая/белая", planned_qty=5,
            ),
            ShipmentPlanLine(
                plan_id=plan.id, warehouse_id=city.id, nomenclature_id=item_blue.id,
                barcode=item_blue.barcode, article="ВзрослаяБазовая/голубой", planned_qty=5,
            ),
        ]
    )
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    # Ровно ОДИН заголовок-модель "ВзрослаяБазовая" (а не два — по одному
    # на каждый цвет, как было бы без разбора '/'), с двумя подгруппами
    # цвета внутри. Полный артикул при этом остается в data-search-text
    # (нужен для поиска), просто больше не показывается как модель.
    assert len(re.findall(r'data-group-level="model"[^>]*data-group-model="ВзрослаяБазовая"', html)) == 1
    assert "Цвет: белая" in html
    assert "Цвет: голубой" in html


def test_split_article_model_color_matches_known_color_without_separator(db):
    """Цвет может идти слитно, без какого-либо разделителя ("К-тSmileбелый")
    — распознается по словарю известных цветов (см. чат), включая
    сокращения/усечения ("бор" вместо "бордо", "олив" вместо "оливковый")."""
    from wms.blueprints.shipment_plan import _split_article_model_color

    assert _split_article_model_color("К-тSmileбелый") == ("К-тSmile", "белый")
    assert _split_article_model_color("К-тSmileголубой") == ("К-тSmile", "голубой")
    assert _split_article_model_color("К-тSmileкорич") == ("К-тSmile", "коричневый")
    assert _split_article_model_color("К-тSmileярко-оранжевый") == ("К-тSmile", "оранжевый")
    assert _split_article_model_color("К-тНатали01бордо") == ("К-тНатали01", "бордовый")
    assert _split_article_model_color("К-тНатали01горчичный") == ("К-тНатали01", "горчичный")
    # Товар без узнаваемого цвета остается моделью без цвета, как раньше.
    assert _split_article_model_color("К-тSmТыковка") == ("К-тSmТыковка", "")
    assert _split_article_model_color("докер") == ("докер", "")
    # '_'-разделитель по-прежнему в приоритете над словарем цветов.
    assert _split_article_model_color("джемпер_жен_молочный_в_беж") == ("джемпер_жен_молочный_в", "беж")
    assert _split_article_model_color("джемпер_жен_молочный_в_бодро") == ("джемпер_жен_молочный_в", "бодро")


def test_picking_groups_merge_no_separator_colors_under_one_model(db, client_logged_in):
    """"К-тSmileбелый" и "К-тSmileголубой" — один и тот же товар в разных
    цветах без какого-либо разделителя, должны объединиться в одну группу
    модели "К-тSmile" с двумя подгруппами цвета."""
    from wms.models import Nomenclature, ShipmentPlan, ShipmentPlanLine, Warehouse

    city = Warehouse(code="WH-NOSEP1", name="ОЗОН: Город", marketplace="ozon", marketplace_city="Город")
    db.session.add(city)
    db.session.commit()

    item_white = Nomenclature(sku="SKU-NOSEP-W", barcode="7770000801", name="Товар белый", unit="шт")
    item_blue = Nomenclature(sku="SKU-NOSEP-B", barcode="7770000802", name="Товар голубой", unit="шт")
    db.session.add_all([item_white, item_blue])
    db.session.commit()

    plan = ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    db.session.commit()
    db.session.add_all(
        [
            ShipmentPlanLine(
                plan_id=plan.id, warehouse_id=city.id, nomenclature_id=item_white.id,
                barcode=item_white.barcode, article="К-тSmileбелый", planned_qty=5,
            ),
            ShipmentPlanLine(
                plan_id=plan.id, warehouse_id=city.id, nomenclature_id=item_blue.id,
                barcode=item_blue.barcode, article="К-тSmileголубой", planned_qty=5,
            ),
        ]
    )
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    assert len(re.findall(r'data-group-level="model"[^>]*data-group-model="К-тSmile"', html)) == 1
    assert "Цвет: белый" in html
    assert "Цвет: голубой" in html


def test_summary_shows_non_empty_box_count_independent_of_plan(db, client_logged_in):
    """"Непустых коробов на складе" в сводке — просто факт по складу (все
    непустые короба, включая те, чьих товаров нет в текущем плане), не
    завязан на picking_list (см. чат: "кол-во коробов отчете и в плане
    разные")."""
    from wms.models import Box, BoxItem, Nomenclature, Warehouse

    main = Warehouse(code="WH-BOXSUM1", name="Основной склад")
    db.session.add(main)
    db.session.commit()

    planned_item, city, _ = _setup(planned_qty=30)
    # planned_item на самом деле создан в другом складе "Склад-отправитель"
    # в _setup — используем main отдельно для контроля состава.
    unrelated_item = Nomenclature(sku="SKU-BOXSUM1", barcode="7770000901", name="Товар вне плана", unit="шт")
    db.session.add(unrelated_item)
    db.session.commit()

    box1 = Box(box_number="BOX-BOXSUM-1", warehouse_id=main.id, status="open")
    box2 = Box(box_number="BOX-BOXSUM-2", warehouse_id=main.id, status="open")
    empty_box = Box(box_number="BOX-BOXSUM-EMPTY", warehouse_id=main.id, status="open")
    db.session.add_all([box1, box2, empty_box])
    db.session.commit()
    db.session.add(BoxItem(box_id=box1.id, nomenclature_id=unrelated_item.id, qty=1))
    db.session.add(BoxItem(box_id=box2.id, nomenclature_id=unrelated_item.id, qty=1))
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    assert "Непустых коробов на складе" in html
    idx = html.find("Основной склад")
    assert idx != -1
    snippet = html[idx : idx + 200]
    assert "<b>2</b>" in snippet


def test_picking_groups_wrap_models_in_category_from_nomenclature(db, client_logged_in):
    """Верхний уровень группировки — Вид товара номенклатуры (Кардиган/
    Шапка/..., см. чат и файл-образец), а не что-то разобранное из
    артикула плана. Модели одной категории объединяются под одним
    заголовком категории."""
    from wms.models import Nomenclature, ProductCategory, ShipmentPlan, ShipmentPlanLine, Warehouse

    category = ProductCategory(name="Кардиганы-ТЕСТ", keywords="кардиганытест")
    db.session.add(category)
    db.session.commit()

    city = Warehouse(code="WH-CAT1", name="ОЗОН: Город", marketplace="ozon", marketplace_city="Город")
    db.session.add(city)
    db.session.commit()

    item_a = Nomenclature(
        sku="SKU-CAT-A", barcode="7770000901", name="Кардиган А", unit="шт", category_id=category.id
    )
    item_b = Nomenclature(
        sku="SKU-CAT-B", barcode="7770000902", name="Кардиган Б", unit="шт", category_id=category.id
    )
    db.session.add_all([item_a, item_b])
    db.session.commit()

    plan = ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    db.session.commit()
    db.session.add_all(
        [
            ShipmentPlanLine(
                plan_id=plan.id, warehouse_id=city.id, nomenclature_id=item_a.id,
                barcode=item_a.barcode, article="Модель_А", planned_qty=5,
            ),
            ShipmentPlanLine(
                plan_id=plan.id, warehouse_id=city.id, nomenclature_id=item_b.id,
                barcode=item_b.barcode, article="Модель_Б", planned_qty=5,
            ),
        ]
    )
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    assert 'data-group-level="category" data-group-category="Кардиганы-ТЕСТ"' in html
    cat_idx = html.find('data-group-category="Кардиганы-ТЕСТ"')
    model_a_idx = html.find('data-group-model="Модель"')
    assert cat_idx != -1
    # Обе модели этой категории идут ПОСЛЕ заголовка категории (внутри
    # ее секции), а не до него.
    assert html.find("Модель_А") > cat_idx or html.find(">Модель<") > cat_idx


def test_picking_groups_fallback_category_for_unmatched_products(db, client_logged_in):
    """Товар без сопоставленной номенклатуры (штрихкод не найден) или без
    вида товара попадает в общую группу "Без категории", а не ломает
    дашборд."""
    from wms.models import ShipmentPlan, ShipmentPlanLine, Warehouse

    city = Warehouse(code="WH-CAT2", name="ОЗОН: Город2", marketplace="ozon", marketplace_city="Город2")
    db.session.add(city)
    db.session.commit()

    plan = ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    db.session.commit()
    db.session.add(
        ShipmentPlanLine(
            plan_id=plan.id, warehouse_id=city.id, nomenclature_id=None,
            barcode="7770000903", article="Неизвестный", planned_qty=5,
        )
    )
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    assert 'data-group-category="Без категории"' in html


def test_totals_row_box_count_is_all_non_empty_boxes_not_only_picking_list(db, client_logged_in):
    """Итоговая строка "Готово к отгрузке" в шапке таблицы — ВСЕ непустые
    короба склада (см. чат: "кол-во непустых коробов неверное"), включая
    короба с товаром, у которого нет невыполненного остатка плана (и
    поэтому сам товар не попадает в picking_list) — не только короба
    строк из "Что нужно отправить". Склад-отправитель обязательно должен
    называться "Основной склад"/"Склад №2 (Шоссейная 167)" — только для
    них считается разбивка "Готово к отгрузке" по складам (см. чат)."""
    from wms.models import Box, BoxItem, Nomenclature, ShipmentPlan, ShipmentPlanLine, Warehouse

    main = Warehouse(code="WH-TOTBOX1", name="Основной склад")
    city = Warehouse(code="WH-TOTBOX2", name="ОЗОН: Город", marketplace="ozon", marketplace_city="Город")
    db.session.add_all([main, city])
    db.session.commit()

    item = Nomenclature(sku="SKU-TOTBOX0", barcode="7770001000", name="Товар в плане", unit="шт")
    db.session.add(item)
    db.session.commit()
    plan = ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    db.session.commit()
    db.session.add(
        ShipmentPlanLine(
            plan_id=plan.id, warehouse_id=city.id, nomenclature_id=item.id,
            barcode=item.barcode, article="ART-TOTBOX", planned_qty=30,
        )
    )
    db.session.commit()

    # Товар ИЗ плана, с невыполненным остатком — попадет в picking_list.
    box_needed = Box(box_number="BOX-TOTBOX-NEEDED", warehouse_id=main.id, status="open")
    db.session.add(box_needed)
    db.session.commit()
    db.session.add(BoxItem(box_id=box_needed.id, nomenclature_id=item.id, qty=5))
    db.session.commit()

    # Товар ВНЕ плана вообще, короб не попадет ни в одну строку picking_list.
    unrelated_item = Nomenclature(sku="SKU-TOTBOX1", barcode="7770001001", name="Товар вне плана", unit="шт")
    db.session.add(unrelated_item)
    db.session.commit()
    box_unrelated = Box(box_number="BOX-TOTBOX-UNRELATED", warehouse_id=main.id, status="open")
    db.session.add(box_unrelated)
    db.session.commit()
    db.session.add(BoxItem(box_id=box_unrelated.id, nomenclature_id=unrelated_item.id, qty=1))
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    idx = html.find("Итого (")
    assert idx != -1
    snippet = html[idx : idx + 800]
    # Оба короба на складе "Основной склад" — 2, а не 1 (сколько бы их ни
    # было в picking_list).
    assert "(2)" in snippet
