"""Приоритет из файла плана отгрузок (колонка "Приоритет", см. чат):

1. Красит строку товара в "Что нужно отправить" на дашборде (0 -> синий,
   1 -> зеленый, 2 -> желтый) — без отдельной колонки под него.
2. Для товаров с проставленным приоритетом при каждой синхронизации плана
   пересчитывает distributed_target_qty на каждую строку — не жесткий
   planned_qty конкретного города, а доля текущего "готово к отгрузке"
   пропорционально доле города в общем плане по этому штрихкоду (см.
   shipment_plan._apply_priority_distribution). ShipmentPlanLine.
   remaining_qty()/effective_planned_qty() используют ее вместо planned_qty,
   когда она посчитана — это и есть то, на чем строится подсказка "куда
   везти короб" (movement._compute_routing)."""

import io

import openpyxl

from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, ShipmentPlan, ShipmentPlanLine, Warehouse


def _plan_file(sheet_name, rows):
    """rows: [(barcode, priority, {city: qty, ...}), ...] — все строки
    одного набора городов (Москва/Казань в тестах ниже)."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet(sheet_name)
    ws.append(["Артикул", "Размер", "Баркод", "Приоритет", "Москва", "Казань"])
    for barcode, priority, qty_by_city in rows:
        ws.append(
            ["ART-1", "44", barcode, priority, qty_by_city.get("Москва", 0), qty_by_city.get("Казань", 0)]
        )
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _upload(client, sheet_name, rows):
    return client.post(
        "/shipment-plan/upload",
        data={"file": (_plan_file(sheet_name, rows), "plan.xlsx")},
        content_type="multipart/form-data",
    )


def _picking_row_tag(html, barcode):
    """Полный открывающий тег <tr ...> строки товара — ищем от начала тега
    до его закрывающего ">", а не просто до первого вхождения штрихкода в
    html (штрихкод встречается и внутри data-search-text раньше, чем
    data-priority, — срез до него обрезал бы половину атрибутов)."""
    idx = html.find(barcode)
    row_start = html.rfind("<tr", 0, idx)
    row_end = html.find(">", row_start)
    return html[row_start:row_end + 1]


def _make_sender_with_packed_stock(item, qty, code="WH-PRIO-SENDER"):
    sender = Warehouse.query.filter_by(code=code).first()
    if not sender:
        sender = Warehouse(code=code, name="Склад-отправитель")
        db.session.add(sender)
        db.session.commit()
    if qty > 0:
        box = Box(box_number=f"BOX-{code}-{item.barcode}", warehouse_id=sender.id, status="open")
        db.session.add(box)
        db.session.commit()
        db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
        db.session.commit()
    return sender


def test_upload_sets_priority_on_created_lines(db, client_logged_in):
    item = Nomenclature(sku="SKU-PRIO-1", barcode="7770200001", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    _make_sender_with_packed_stock(item, qty=0)

    _upload(
        client_logged_in,
        "Распределение ОЗОН ФБС от 01.09",
        [("7770200001", 1, {"Москва": 10, "Казань": 5})],
    )

    lines = ShipmentPlanLine.query.filter_by(barcode="7770200001").all()
    assert len(lines) == 2
    assert all(line.priority == 1 for line in lines)


def test_upload_without_priority_column_leaves_priority_none(db, client_logged_in):
    item = Nomenclature(sku="SKU-PRIO-1B", barcode="7770200099", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("Распределение ОЗОН ФБС от 01.09")
    ws.append(["Артикул", "Размер", "Баркод", "Москва"])
    ws.append(["ART-1", "44", "7770200099", 10])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    client_logged_in.post(
        "/shipment-plan/upload",
        data={"file": (buf, "plan.xlsx")},
        content_type="multipart/form-data",
    )

    line = ShipmentPlanLine.query.filter_by(barcode="7770200099").first()
    assert line.priority is None
    assert line.distributed_target_qty is None


def test_priority_distribution_splits_ready_to_ship_proportionally_to_plan_share(db, client_logged_in):
    """План: Москва 100, Казань 50 (доля 2:1). Готово к отгрузке — 60 (меньше
    всего плана 150) -> распределяем по той же пропорции: Москва 40,
    Казань 20 (см. чат)."""
    item = Nomenclature(sku="SKU-PRIO-2", barcode="7770200002", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    _make_sender_with_packed_stock(item, qty=60, code="WH-PRIO-2")

    _upload(
        client_logged_in,
        "Распределение ОЗОН ФБС от 01.09",
        [("7770200002", 1, {"Москва": 100, "Казань": 50})],
    )

    moscow = ShipmentPlanLine.query.filter_by(barcode="7770200002").join(Warehouse).filter(
        Warehouse.marketplace_city == "Москва"
    ).first()
    kazan = ShipmentPlanLine.query.filter_by(barcode="7770200002").join(Warehouse).filter(
        Warehouse.marketplace_city == "Казань"
    ).first()

    assert moscow.distributed_target_qty == 40.0
    assert kazan.distributed_target_qty == 20.0


def test_priority_distribution_handles_surplus_ready_to_ship_beyond_plan(db, client_logged_in):
    """Готово к отгрузке (180) БОЛЬШЕ, чем весь план (150) — избыток тоже
    распределяется по пропорции плана, а не только полностью план."""
    item = Nomenclature(sku="SKU-PRIO-3", barcode="7770200003", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    _make_sender_with_packed_stock(item, qty=180, code="WH-PRIO-3")

    _upload(
        client_logged_in,
        "Распределение ОЗОН ФБС от 01.09",
        [("7770200003", 2, {"Москва": 100, "Казань": 50})],
    )

    moscow = ShipmentPlanLine.query.filter_by(barcode="7770200003").join(Warehouse).filter(
        Warehouse.marketplace_city == "Москва"
    ).first()
    kazan = ShipmentPlanLine.query.filter_by(barcode="7770200003").join(Warehouse).filter(
        Warehouse.marketplace_city == "Казань"
    ).first()

    assert moscow.distributed_target_qty == 120.0
    assert kazan.distributed_target_qty == 60.0


def test_priority_distribution_skipped_for_items_without_priority(db, client_logged_in):
    item = Nomenclature(sku="SKU-PRIO-4", barcode="7770200004", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    _make_sender_with_packed_stock(item, qty=60, code="WH-PRIO-4")

    _upload(
        client_logged_in,
        "Распределение ОЗОН ФБС от 01.09",
        [("7770200004", None, {"Москва": 100, "Казань": 50})],
    )

    lines = ShipmentPlanLine.query.filter_by(barcode="7770200004").all()
    assert all(line.distributed_target_qty is None for line in lines)


def test_priority_distribution_zero_planned_total_gives_zero_target(db, client_logged_in):
    """priority задан, но план по всем городам этого штрихкода нулевой —
    делить нечего, не должно падать делением на ноль."""
    item = Nomenclature(sku="SKU-PRIO-5", barcode="7770200005", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    _make_sender_with_packed_stock(item, qty=30, code="WH-PRIO-5")

    plan = ShipmentPlan.query.filter_by(marketplace="ozon").first() or ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    city = Warehouse(code="WH-PRIO-5-CITY", name="ОЗОН: Тверь", marketplace="ozon", marketplace_city="Тверь")
    db.session.add(city)
    db.session.commit()
    line = ShipmentPlanLine(
        plan_id=plan.id, warehouse_id=city.id, nomenclature_id=item.id,
        barcode="7770200005", planned_qty=0, priority=1,
    )
    db.session.add(line)
    db.session.commit()

    from wms.blueprints.shipment_plan import _apply_priority_distribution

    _apply_priority_distribution()
    db.session.commit()

    assert ShipmentPlanLine.query.get(line.id).distributed_target_qty == 0.0


def test_effective_planned_qty_uses_distributed_target_when_priority_set(db):
    line = ShipmentPlanLine(
        plan_id=1, warehouse_id=1, barcode="X", planned_qty=100, fulfilled_qty=0,
        priority=1, distributed_target_qty=40,
    )
    assert line.effective_planned_qty() == 40


def test_effective_planned_qty_falls_back_to_planned_when_not_yet_distributed(db):
    line = ShipmentPlanLine(
        plan_id=1, warehouse_id=1, barcode="X", planned_qty=100, fulfilled_qty=0,
        priority=1, distributed_target_qty=None,
    )
    assert line.effective_planned_qty() == 100


def test_effective_planned_qty_ignores_distributed_target_without_priority(db):
    line = ShipmentPlanLine(
        plan_id=1, warehouse_id=1, barcode="X", planned_qty=100, fulfilled_qty=0,
        priority=None, distributed_target_qty=40,
    )
    assert line.effective_planned_qty() == 100


def test_remaining_qty_still_uses_plain_planned_qty_for_priority_items(db):
    """remaining_qty() (используется в отображении дашборда) сознательно НЕ
    переключается на distributed_target_qty — иначе товар с priority и
    нулевым "готово к отгрузке" пропал бы из "Что нужно отправить" (см.
    effective_planned_qty docstring и чат). Целевое распределение влияет
    только на подсказку маршрутизации короба (movement._compute_routing)."""
    line = ShipmentPlanLine(
        plan_id=1, warehouse_id=1, barcode="X", planned_qty=100, fulfilled_qty=0,
        priority=1, distributed_target_qty=0,
    )
    assert line.remaining_qty() == 100


def test_dashboard_highlights_row_green_for_priority_one(db, client_logged_in):
    item = Nomenclature(sku="SKU-PRIO-6", barcode="7770200006", name="Товар приоритетный", unit="шт")
    db.session.add(item)
    db.session.commit()
    _make_sender_with_packed_stock(item, qty=10, code="WH-PRIO-6")

    _upload(client_logged_in, "Распределение ОЗОН ФБС от 01.09", [("7770200006", 1, {"Москва": 10})])

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    idx = html.find("7770200006")
    row_start = html.rfind("<tr", 0, idx)
    row_html = html[row_start:idx]
    assert "table-success" in row_html


def test_dashboard_highlights_row_yellow_for_priority_two(db, client_logged_in):
    item = Nomenclature(sku="SKU-PRIO-7", barcode="7770200007", name="Товар приоритетный 2", unit="шт")
    db.session.add(item)
    db.session.commit()
    _make_sender_with_packed_stock(item, qty=10, code="WH-PRIO-7")

    _upload(client_logged_in, "Распределение ОЗОН ФБС от 01.09", [("7770200007", 2, {"Москва": 10})])

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    idx = html.find("7770200007")
    row_start = html.rfind("<tr", 0, idx)
    row_html = html[row_start:idx]
    assert "table-warning" in row_html


def test_dashboard_highlights_row_blue_for_priority_zero(db, client_logged_in):
    item = Nomenclature(sku="SKU-PRIO-8", barcode="7770200008", name="Товар приоритетный 3", unit="шт")
    db.session.add(item)
    db.session.commit()
    _make_sender_with_packed_stock(item, qty=10, code="WH-PRIO-8")

    _upload(client_logged_in, "Распределение ОЗОН ФБС от 01.09", [("7770200008", 0, {"Москва": 10})])

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    idx = html.find("7770200008")
    row_start = html.rfind("<tr", 0, idx)
    row_html = html[row_start:idx]
    assert "table-info" in row_html


def test_dashboard_no_highlight_without_priority(db, client_logged_in):
    item = Nomenclature(sku="SKU-PRIO-9", barcode="7770200009", name="Товар без приоритета", unit="шт")
    db.session.add(item)
    db.session.commit()
    _make_sender_with_packed_stock(item, qty=10, code="WH-PRIO-9")

    _upload(client_logged_in, "Распределение ОЗОН ФБС от 01.09", [("7770200009", None, {"Москва": 10})])

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    idx = html.find("7770200009")
    row_start = html.rfind("<tr", 0, idx)
    row_html = html[row_start:idx]
    assert "table-success" not in row_html
    assert "table-warning" not in row_html
    assert "table-info" not in row_html


def test_dashboard_no_stock_wins_over_priority_color(db, client_logged_in):
    """no_stock (нет на складе-отправителе) красит строку красным даже у
    приоритетного товара — важнее цвета приоритета (см. чат)."""
    item = Nomenclature(sku="SKU-PRIO-10", barcode="7770200010", name="Товар без остатка", unit="шт")
    db.session.add(item)
    db.session.commit()
    # Ничего не упаковано на складе-отправителе -> no_stock = True.
    _make_sender_with_packed_stock(item, qty=0, code="WH-PRIO-10")

    _upload(client_logged_in, "Распределение ОЗОН ФБС от 01.09", [("7770200010", 1, {"Москва": 10})])

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    idx = html.find("7770200010")
    row_start = html.rfind("<tr", 0, idx)
    row_html = html[row_start:idx]
    assert "table-danger" in row_html
    assert "table-success" not in row_html


def test_dashboard_has_priority_only_checkbox(db, client_logged_in):
    """Галочка — часть карточки "Что нужно отправить", которая рендерится
    только когда в picking_list вообще есть позиции (см. {% if
    picking_list %} в шаблоне), поэтому тесту нужен хотя бы один товар."""
    item = Nomenclature(sku="SKU-PRIO-13", barcode="7770200013", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    _make_sender_with_packed_stock(item, qty=10, code="WH-PRIO-13")
    _upload(client_logged_in, "Распределение ОЗОН ФБС от 01.09", [("7770200013", None, {"Москва": 10})])

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    assert 'data-picking-priority-only="picking"' in html
    assert "Приоритетные товары" in html


def test_dashboard_row_carries_priority_data_attribute(db, client_logged_in):
    item = Nomenclature(sku="SKU-PRIO-11", barcode="7770200011", name="Товар с приоритетом", unit="шт")
    db.session.add(item)
    db.session.commit()
    _make_sender_with_packed_stock(item, qty=10, code="WH-PRIO-11")

    _upload(client_logged_in, "Распределение ОЗОН ФБС от 01.09", [("7770200011", 1, {"Москва": 10})])

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    assert 'data-priority="1"' in _picking_row_tag(html, "7770200011")


def test_dashboard_row_without_priority_has_empty_priority_attribute(db, client_logged_in):
    item = Nomenclature(sku="SKU-PRIO-12", barcode="7770200012", name="Товар без приоритета", unit="шт")
    db.session.add(item)
    db.session.commit()
    _make_sender_with_packed_stock(item, qty=10, code="WH-PRIO-12")

    _upload(client_logged_in, "Распределение ОЗОН ФБС от 01.09", [("7770200012", None, {"Москва": 10})])

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    assert 'data-priority=""' in _picking_row_tag(html, "7770200012")
