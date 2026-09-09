"""Штрихкод короба: Box.barcode_value кодирует только цифры (см. коммит
про Code128 и переключение наборов символов), а Box.find_by_scanned_code
должен принимать как этот цифровой код, так и полный номер вручную."""

from wms.extensions import db
from wms.models import Box, Warehouse


def _make_warehouse():
    wh = Warehouse(code="WH-T", name="Тестовый склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def test_barcode_value_is_digits_only(db):
    wh = _make_warehouse()
    box = Box(box_number="BOX-000123", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()

    assert box.barcode_value == "000123"


def test_find_by_scanned_code_accepts_full_number(db):
    wh = _make_warehouse()
    box = Box(box_number="BOX-000123", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()

    assert Box.find_by_scanned_code("BOX-000123").id == box.id


def test_find_by_scanned_code_accepts_digits_only(db):
    wh = _make_warehouse()
    box = Box(box_number="BOX-000123", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()

    assert Box.find_by_scanned_code("000123").id == box.id


def test_find_by_scanned_code_respects_warehouse_filter(db):
    wh1 = Warehouse(code="WH-1", name="Склад 1")
    wh2 = Warehouse(code="WH-2", name="Склад 2")
    db.session.add_all([wh1, wh2])
    db.session.commit()
    box = Box(box_number="BOX-000555", warehouse_id=wh1.id, status="open")
    db.session.add(box)
    db.session.commit()

    assert Box.find_by_scanned_code("000555", warehouse_id=wh1.id) is not None
    assert Box.find_by_scanned_code("000555", warehouse_id=wh2.id) is None


def test_find_by_scanned_code_no_match_returns_none(db):
    _make_warehouse()
    assert Box.find_by_scanned_code("999999") is None
    assert Box.find_by_scanned_code("") is None


def test_find_by_scanned_code_digit_scan_does_not_full_scan(db):
    """Раньше цифровой код (реальный скан штрихкода) ВСЕГДА проваливался в
    LIKE '%code' с ведущим '%' — такой LIKE не может использовать индекс и
    означает полное сканирование таблицы boxes на каждый скан короба. При
    десятках-сотнях тысяч коробов это и давало заметные тормоза в приемке/
    размещении/перемещении. Проверяем, что для штатного случая (код той же
    ширины, что и в номерах коробов) запрос идет точным совпадением, а не
    LIKE — соответствующий SQL не должен встречаться вовсе."""
    wh = _make_warehouse()
    box = Box(box_number="BOX-000123", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()

    from sqlalchemy import event

    statements = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", _capture)
    try:
        found = Box.find_by_scanned_code("000123")
    finally:
        event.remove(db.engine, "before_cursor_execute", _capture)

    assert found.id == box.id
    assert not any("LIKE" in s.upper() for s in statements), statements
