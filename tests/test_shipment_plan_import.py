"""Разбор файла плана отгрузок. Формат листов не фиксирован жестко (см.
wms/utils/shipment_plan_import.py) — эти тесты фиксируют два реальных
случая, всплывших на боевом файле: несколько листов "Распределение..."
на один маркетплейс должны объединяться, а лишний факт без плана — не
теряться."""

import io

import openpyxl

from wms.utils.shipment_plan_import import extract_period_start, parse_plan_sheet


def _sheet_to_bytes(rows_by_sheet):
    """rows_by_sheet: {sheet_name: [[cell, cell, ...], ...]}"""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in rows_by_sheet.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def test_extract_period_start_parses_date_from_sheet_name():
    assert extract_period_start("Распределение ОЗОН ФБС от 27.08") is not None


def test_extract_period_start_none_when_no_date():
    assert extract_period_start("Распределение ОЗОН ФБС") is None


def test_parse_single_sheet():
    data = _sheet_to_bytes(
        {
            "Распределение ОЗОН ФБС от 01.09": [
                ["Артикул", "Размер", "Баркод", "Москва", "Питер"],
                ["A1", "46", "1111", 5, 2],
            ]
        }
    )

    plan = parse_plan_sheet(data, "ozon")

    assert plan is not None
    assert plan.cities == ["Москва", "Питер"]
    assert len(plan.rows) == 2
    assert {r["city"]: r["qty"] for r in plan.rows} == {"Москва": 5.0, "Питер": 2.0}


def test_parse_merges_multiple_sheets_for_same_marketplace():
    """Если в файле два листа "Распределение..." под один и тот же
    маркетплейс (например, основная выгрузка + отдельная категория) —
    оба должны попасть в план, а не только первый найденный по имени."""
    data = _sheet_to_bytes(
        {
            "Распределение ОЗОН ФБС от 01.09": [
                ["Артикул", "Размер", "Баркод", "Москва"],
                ["A1", "46", "1111", 5],
            ],
            "Распределение ОЗОН доп от 01.09": [
                ["Артикул", "Размер", "Баркод", "Казань"],
                ["A2", "48", "2222", 7],
            ],
        }
    )

    plan = parse_plan_sheet(data, "ozon")

    assert plan is not None
    assert set(plan.cities) == {"Москва", "Казань"}
    assert len(plan.rows) == 2


def test_parse_ignores_sheets_of_other_marketplace():
    data = _sheet_to_bytes(
        {
            "Распределение ВБ ФБС от 01.09": [
                ["Артикул", "Размер", "Баркод", "Москва"],
                ["A1", "46", "1111", 5],
            ]
        }
    )

    assert parse_plan_sheet(data, "ozon") is None


def test_parse_returns_none_when_no_matching_sheet():
    data = _sheet_to_bytes(
        {
            "Прочий лист": [["что-то"]],
        }
    )

    assert parse_plan_sheet(data, "ozon") is None
