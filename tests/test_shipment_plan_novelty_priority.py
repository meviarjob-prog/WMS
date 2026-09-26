"""Новые типы приоритета в плане отгрузок (см. чат: "введем еще типы
приоритетов. 0w - это новинки только для вб, 0o- это новинки только для
ozon"):

1. Код "0w"/"0o" в колонке "Приоритет" — товар-новинка, у которого еще нет
   собственного плана ни по одному городу. Такая строка НЕ отбрасывается
   парсером, даже если план (и факт) пуст по всем городам, — иначе сам
   код приоритета было бы негде хранить (см.
   utils.shipment_plan_import._parse_priority_cell).
2. "Если по товару нет плана — смотрим на приоритет": для такого товара
   distributed_target_qty считается не от доли ЕГО ЖЕ плана (взять неоткуда
   — плана нет), а от среднего процента распределения ОСТАЛЬНЫХ товаров
   этого одного маркетплейса (shipment_plan._average_city_share_by_marketplace)
   — и только между городами указанного маркетплейса."""

import io

import openpyxl

from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, ShipmentPlanLine, Warehouse
from wms.utils.shipment_plan_import import _parse_priority_cell


def _plan_file(sheet_name, rows):
    """rows: [(barcode, priority, {city: qty, ...}), ...]."""
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


def _make_sender_with_packed_stock(item, qty, code):
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


def _line(barcode, city):
    return (
        ShipmentPlanLine.query.filter_by(barcode=barcode)
        .join(Warehouse)
        .filter(Warehouse.marketplace_city == city)
        .first()
    )


def test_parse_priority_cell_standard_integers():
    assert _parse_priority_cell(0) == (0, None)
    assert _parse_priority_cell(1) == (1, None)
    assert _parse_priority_cell(2) == (2, None)


def test_parse_priority_cell_novelty_codes_case_and_space_insensitive():
    assert _parse_priority_cell("0w") == (None, "wb")
    assert _parse_priority_cell("0W") == (None, "wb")
    assert _parse_priority_cell(" 0o ") == (None, "ozon")
    assert _parse_priority_cell("0O") == (None, "ozon")


def test_parse_priority_cell_blank_or_unreadable():
    assert _parse_priority_cell(None) == (None, None)
    assert _parse_priority_cell("") == (None, None)
    assert _parse_priority_cell("абв") == (None, None)


def test_novelty_row_survives_import_despite_empty_plan_on_every_city(db, client_logged_in):
    item = Nomenclature(sku="SKU-NOV-1", barcode="7770300001", name="Новинка", unit="шт")
    db.session.add(item)
    db.session.commit()

    _upload(
        client_logged_in,
        "Распределение ВБ ФБС от 01.09",
        [("7770300001", "0w", {})],
    )

    lines = ShipmentPlanLine.query.filter_by(barcode="7770300001").all()
    assert len(lines) == 2  # Москва и Казань — обе строки, а не ни одной
    assert all(line.novelty_marketplace == "wb" for line in lines)
    assert all(line.priority is None for line in lines)
    assert all(line.planned_qty == 0 for line in lines)


def test_ordinary_row_without_priority_and_empty_plan_is_still_dropped(db, client_logged_in):
    """Регресс: поведение для обычных (не 0w/0o) строк без плана не должно
    было измениться — пустая строка без приоритета по-прежнему не создает
    ShipmentPlanLine ни по одному городу."""
    item = Nomenclature(sku="SKU-NOV-2", barcode="7770300002", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    _upload(
        client_logged_in,
        "Распределение ВБ ФБС от 01.09",
        [("7770300002", None, {})],
    )

    assert ShipmentPlanLine.query.filter_by(barcode="7770300002").count() == 0


def test_novelty_distribution_uses_average_city_share_of_other_wb_items(db, client_logged_in):
    """X: Москва 80 / Казань 20 (доля 0.8/0.2). Y: Москва 20 / Казань 80
    (доля 0.2/0.8). Средняя доля — 50/50. Новинка Z с готово-к-отгрузке=60
    делится по этой средней доле: 30/30 (см. чат)."""
    item_x = Nomenclature(sku="SKU-NOV-X", barcode="7770300010", name="Товар X", unit="шт")
    item_y = Nomenclature(sku="SKU-NOV-Y", barcode="7770300011", name="Товар Y", unit="шт")
    item_z = Nomenclature(sku="SKU-NOV-Z", barcode="7770300012", name="Новинка Z", unit="шт")
    db.session.add_all([item_x, item_y, item_z])
    db.session.commit()
    _make_sender_with_packed_stock(item_z, qty=60, code="WH-NOV-Z")

    _upload(
        client_logged_in,
        "Распределение ВБ ФБС от 01.09",
        [
            ("7770300010", None, {"Москва": 80, "Казань": 20}),
            ("7770300011", None, {"Москва": 20, "Казань": 80}),
            ("7770300012", "0w", {}),
        ],
    )

    moscow = _line("7770300012", "Москва")
    kazan = _line("7770300012", "Казань")
    assert moscow.distributed_target_qty == 30.0
    assert kazan.distributed_target_qty == 30.0


def test_novelty_ignores_other_marketplace_lines_of_same_barcode(db, client_logged_in):
    """Штрихкод новинки (0w) вдруг нашелся и в плане ОЗОН (например, старые
    данные) — код "только ВБ" должен обнулить именно ozon-строку, а не
    подмешать ее в распределение."""
    item_x = Nomenclature(sku="SKU-NOV-X2", barcode="7770300020", name="Товар X2", unit="шт")
    item_y = Nomenclature(sku="SKU-NOV-Y2", barcode="7770300021", name="Товар Y2", unit="шт")
    item_z = Nomenclature(sku="SKU-NOV-Z2", barcode="7770300022", name="Новинка Z2", unit="шт")
    db.session.add_all([item_x, item_y, item_z])
    db.session.commit()
    _make_sender_with_packed_stock(item_z, qty=60, code="WH-NOV-Z2")

    _upload(
        client_logged_in,
        "Распределение ВБ ФБС от 01.09",
        [
            ("7770300020", None, {"Москва": 80, "Казань": 20}),
            ("7770300021", None, {"Москва": 20, "Казань": 80}),
            ("7770300022", "0w", {}),
        ],
    )
    _upload(
        client_logged_in,
        "Распределение ОЗОН ФБС от 01.09",
        [("7770300022", None, {"Москва": 50})],
    )

    ozon_line = (
        ShipmentPlanLine.query.filter_by(barcode="7770300022")
        .join(Warehouse)
        .filter(Warehouse.marketplace == "ozon")
        .first()
    )
    wb_moscow = _line("7770300022", "Москва")
    wb_kazan = _line("7770300022", "Казань")

    assert ozon_line.distributed_target_qty == 0.0
    assert wb_moscow.distributed_target_qty == 30.0
    assert wb_kazan.distributed_target_qty == 30.0


def test_novelty_falls_back_to_equal_split_without_any_other_wb_data(db, client_logged_in):
    """Нет вообще других товаров ВБ, чтобы посчитать средний процент —
    делим поровну между городами этой площадки, а не падаем/обнуляем."""
    item = Nomenclature(sku="SKU-NOV-3", barcode="7770300030", name="Новинка", unit="шт")
    db.session.add(item)
    db.session.commit()
    _make_sender_with_packed_stock(item, qty=40, code="WH-NOV-3")

    _upload(
        client_logged_in,
        "Распределение ВБ ФБС от 01.09",
        [("7770300030", "0w", {})],
    )

    moscow = _line("7770300030", "Москва")
    kazan = _line("7770300030", "Казань")
    assert moscow.distributed_target_qty == 20.0
    assert kazan.distributed_target_qty == 20.0


def test_effective_planned_qty_uses_distributed_target_for_novelty(db, client_logged_in):
    item = Nomenclature(sku="SKU-NOV-4", barcode="7770300040", name="Новинка", unit="шт")
    db.session.add(item)
    db.session.commit()
    _make_sender_with_packed_stock(item, qty=40, code="WH-NOV-4")

    _upload(
        client_logged_in,
        "Распределение ВБ ФБС от 01.09",
        [("7770300040", "0w", {})],
    )

    moscow = _line("7770300040", "Москва")
    assert moscow.planned_qty == 0
    assert moscow.effective_planned_qty() == 20.0


def test_dashboard_shows_novelty_badge_and_survives_zero_plan_filter(db, client_logged_in):
    item = Nomenclature(sku="SKU-NOV-5", barcode="7770300050", name="Тестовый товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    _make_sender_with_packed_stock(item, qty=15, code="WH-NOV-5")

    _upload(
        client_logged_in,
        "Распределение ВБ ФБС от 01.09",
        [("7770300050", "0w", {})],
    )

    resp = client_logged_in.get("/shipment-plan/")
    html = resp.get_data(as_text=True)
    assert "7770300050" in html
    assert "Новинка ВБ" in html  # бейджик маркетплейса, не название товара
    idx = html.find("7770300050")
    row_start = html.rfind("<tr", 0, idx)
    row_end = html.find(">", row_start)
    row_tag = html[row_start:row_end + 1]
    assert 'data-priority="wb"' in row_tag
