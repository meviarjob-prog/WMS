"""Список размещения теперь два таба — "Разместить товар" (неразмещенный
остаток) и "Разместить короб" (короба без ячейки), см. чат — и обе таблицы
постраничные (PLACEMENT_PAGE_SIZE в wms/blueprints/placement.py), т.к. на
активном складе они могут разрастись до тысяч строк и тормозить страницу
целиком, если рендерить всё разом."""

from wms.extensions import db
from wms.models import Box, Nomenclature, PlacementDocument, UnplacedStock, Warehouse
from wms.blueprints.placement import PLACEMENT_PAGE_SIZE


def _make_warehouse(suffix):
    wh = Warehouse(code=f"WH-PLPG{suffix}", name="Склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def test_list_page_shows_both_tabs(db, client_logged_in):
    html = client_logged_in.get("/placement/").get_data(as_text=True)

    assert "📦 Разместить товар" in html
    assert "🗄 Разместить короб" in html
    assert 'id="tab-item"' in html
    assert 'id="tab-box"' in html


def test_unplaced_stock_is_paginated(db, client_logged_in):
    wh = _make_warehouse("1")
    for i in range(PLACEMENT_PAGE_SIZE + 5):
        item = Nomenclature(sku=f"SKU-PLPG1-{i}", barcode=f"777081{i:05d}", name=f"Товар {i}", unit="шт")
        db.session.add(item)
        db.session.commit()
        db.session.add(UnplacedStock(warehouse_id=wh.id, nomenclature_id=item.id, qty=1))
    db.session.commit()

    page1 = client_logged_in.get("/placement/").get_data(as_text=True)
    assert "Стр. 1 из 2" in page1
    assert f"всего строк: {PLACEMENT_PAGE_SIZE + 5}" in page1

    page2 = client_logged_in.get("/placement/?stock_page=2").get_data(as_text=True)
    assert "Стр. 2 из 2" in page2


def test_open_boxes_are_paginated(db, client_logged_in):
    wh = _make_warehouse("2")
    for i in range(PLACEMENT_PAGE_SIZE + 3):
        db.session.add(Box(box_number=f"BOX-PLPG2-{i}", warehouse_id=wh.id, status="open"))
    db.session.commit()

    page1 = client_logged_in.get("/placement/").get_data(as_text=True)
    assert "Стр. 1 из 2" in page1
    assert f"всего коробов: {PLACEMENT_PAGE_SIZE + 3}" in page1

    page2 = client_logged_in.get("/placement/?boxes_page=2").get_data(as_text=True)
    assert "Стр. 2 из 2" in page2


def test_document_status_badges_renamed(db, client_logged_in):
    wh = _make_warehouse("3")
    draft = PlacementDocument(number="PL-PG-0001", warehouse_id=wh.id, status="draft")
    completed = PlacementDocument(number="PL-PG-0002", warehouse_id=wh.id, status="completed")
    db.session.add_all([draft, completed])
    db.session.commit()

    html = client_logged_in.get("/placement/").get_data(as_text=True)

    assert "В работе" in html
    assert "Завершено" in html
    assert "Черновик" not in html
