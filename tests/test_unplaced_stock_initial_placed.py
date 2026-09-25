"""Колонки "Начальный остаток" и "Размещено" на странице Размещения (см.
чат: "в неразмещенном остатке нужна колонка начальных остатков и сколько
размещено в короба") — считаются по активным партиям (UnplacedStockLot),
из которых складывается текущий остаток (см. UnplacedStock.active_lots())."""

from wms.extensions import db
from wms.models import Nomenclature, UnplacedStock, Warehouse


def _make_warehouse(code="WH-IP"):
    wh = Warehouse(code=code, name="Тест склад начальных остатков")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(barcode, name="Товар начальный остаток"):
    item = Nomenclature(sku=barcode, barcode=barcode, name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def test_initial_and_placed_qty_reflect_partial_consumption(db):
    wh = _make_warehouse("WH-IP-1")
    item = _make_item("7772000001")

    UnplacedStock.add(wh.id, item.id, 10)
    db.session.commit()
    UnplacedStock.consume(wh.id, item.id, 4)
    db.session.commit()

    row = UnplacedStock.query.filter_by(warehouse_id=wh.id, nomenclature_id=item.id).first()
    assert row.initial_qty() == 10
    assert row.placed_qty() == 4
    assert row.qty == 6


def test_initial_and_placed_qty_none_without_lots(db):
    """Остаток, заведенный до появления партий (или напрямую в БД, минуя
    UnplacedStock.add()) — начальное количество неизвестно, не 0."""
    wh = _make_warehouse("WH-IP-2")
    item = _make_item("7772000002")
    row = UnplacedStock(warehouse_id=wh.id, nomenclature_id=item.id, qty=7)
    db.session.add(row)
    db.session.commit()

    assert row.initial_qty() is None
    assert row.placed_qty() is None


def test_initial_qty_only_counts_lots_still_contributing_to_current_balance(db):
    """Партия, которую уже разместили полностью, больше не часть текущего
    остатка — не должна раздувать "начальный остаток" новой, отдельной
    партии, пришедшей позже."""
    wh = _make_warehouse("WH-IP-3")
    item = _make_item("7772000003")

    UnplacedStock.add(wh.id, item.id, 5)  # первая партия
    db.session.commit()
    UnplacedStock.consume(wh.id, item.id, 5)  # полностью размещена
    db.session.commit()

    UnplacedStock.add(wh.id, item.id, 8)  # вторая, отдельная партия
    db.session.commit()

    row = UnplacedStock.query.filter_by(warehouse_id=wh.id, nomenclature_id=item.id).first()
    assert row.initial_qty() == 8  # не 13
    assert row.placed_qty() == 0
    assert row.qty == 8


def test_placement_list_page_shows_initial_and_placed_columns(db, client_logged_in):
    wh = _make_warehouse("WH-IP-4")
    item = _make_item("7772000004")
    UnplacedStock.add(wh.id, item.id, 10)
    db.session.commit()
    UnplacedStock.consume(wh.id, item.id, 3)
    db.session.commit()

    resp = client_logged_in.get("/placement/")
    html = resp.get_data(as_text=True)

    assert "Начальный остаток" in html
    assert "Размещено" in html
    assert item.barcode in html


def test_placement_list_page_shows_dash_for_row_without_lots(db, client_logged_in):
    wh = _make_warehouse("WH-IP-5")
    item = _make_item("7772000005")
    row = UnplacedStock(warehouse_id=wh.id, nomenclature_id=item.id, qty=9)
    db.session.add(row)
    db.session.commit()

    resp = client_logged_in.get("/placement/")
    html = resp.get_data(as_text=True)
    assert item.barcode in html
