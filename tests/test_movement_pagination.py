"""Список перемещений выводится страницами по 200 документов."""

from datetime import datetime, timedelta

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, MovementLine, Nomenclature, Warehouse


def _create_documents(count):
    sender = Warehouse(code="WH-PAGE-A", name="Основной")
    destination = Warehouse(code="WH-PAGE-B", name="Москва")
    db.session.add_all([sender, destination])
    db.session.flush()
    start = datetime(2026, 1, 1)
    documents = []
    for index in range(count):
        document = MovementDocument(
            number=f"PER-PAGE-{index + 1:03d}",
            from_warehouse_id=sender.id,
            to_warehouse_id=destination.id,
            created_at=start + timedelta(minutes=index),
        )
        db.session.add(document)
        documents.append(document)
    db.session.commit()
    return documents


def test_movement_list_is_paginated_by_two_hundred(db, client_logged_in):
    _create_documents(201)

    first_page = client_logged_in.get("/movement/").get_data(as_text=True)
    assert first_page.count('class="movement-number text-decoration-none"') == 200
    assert "PER-PAGE-201" in first_page
    assert "PER-PAGE-002" in first_page
    assert "PER-PAGE-001" not in first_page
    assert "Страница 1 из 2" in first_page
    assert "всего перемещений: 201" in first_page
    assert "Экспорт в Excel" not in first_page

    second_page = client_logged_in.get("/movement/?page=2").get_data(as_text=True)
    assert second_page.count('class="movement-number text-decoration-none"') == 1
    assert "PER-PAGE-001" in second_page
    assert "PER-PAGE-002" not in second_page
    assert "Страница 2 из 2" in second_page


def test_movement_pagination_is_hidden_for_one_page(db, client_logged_in):
    _create_documents(2)
    html = client_logged_in.get("/movement/").get_data(as_text=True)
    assert "Страница 1 из" not in html
    assert 'class="container-fluid px-4 app-content"' in html
    assert "mobile-compact-list-page" in html
    assert "movement-list-card" in html
    assert "movement-list-item" in html


def test_search_by_number_finds_document_on_another_page(db, client_logged_in):
    """Поиск идет запросом к БД (см. movement._apply_movement_search), а не
    JS-фильтром по уже загруженным строкам — должен находить документ,
    который сам по себе не попадает на первую страницу (см. чат)."""
    _create_documents(201)

    html = client_logged_in.get("/movement/?q=PER-PAGE-001").get_data(as_text=True)

    assert "PER-PAGE-001" in html
    assert "PER-PAGE-201" not in html
    assert 'value="PER-PAGE-001"' in html


def test_search_matches_warehouse_name(db, client_logged_in):
    _create_documents(3)

    html = client_logged_in.get("/movement/?q=Москва").get_data(as_text=True)

    assert "PER-PAGE-001" in html
    assert "PER-PAGE-002" in html
    assert "PER-PAGE-003" in html


def test_search_no_match_shows_empty_list(db, client_logged_in):
    _create_documents(3)

    html = client_logged_in.get("/movement/?q=NOTHING-HERE").get_data(as_text=True)

    assert "PER-PAGE-001" not in html
    assert "Перемещений еще нет" in html


def test_mp_request_filter_narrows_across_all_documents(db, client_logged_in):
    documents = _create_documents(3)
    documents[0].marketplace_request_number = "REQ-1"
    documents[0].marketplace_request_created_at = datetime(2026, 1, 2)
    db.session.commit()

    html = client_logged_in.get("/movement/?mp_request=yes").get_data(as_text=True)

    assert "PER-PAGE-001" in html
    assert "PER-PAGE-002" not in html
    assert "PER-PAGE-003" not in html


def _make_box_document(number, box_number, sender_name, dest_name, dest_marketplace=None):
    sender = Warehouse(code=f"WH-MULTI-{number}A", name=sender_name)
    dest = Warehouse(
        code=f"WH-MULTI-{number}B",
        name=dest_name,
        marketplace=dest_marketplace,
        marketplace_city=dest_name if dest_marketplace else None,
    )
    db.session.add_all([sender, dest])
    db.session.commit()

    item = Nomenclature(sku=f"SKU-MULTI-{number}", barcode=f"7770910{number}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    box = Box(box_number=box_number, warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=1))

    doc = MovementDocument(number=number, from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
    db.session.commit()
    return doc


def test_search_with_two_words_matches_across_different_fields(db, client_logged_in):
    """«191 москва» — оба слова должны совпасть, но не обязательно в одном
    и том же поле: "191" в номере короба, "москва" в складе назначения
    (см. чат)."""
    matching = _make_box_document(
        "PER-MULTI-001", "BOX-191-A", "Основной", "Москва", dest_marketplace="ozon"
    )
    other_city = _make_box_document(
        "PER-MULTI-002", "BOX-191-B", "Основной", "Казань", dest_marketplace="ozon"
    )
    other_box = _make_box_document(
        "PER-MULTI-003", "BOX-777-C", "Основной", "Москва", dest_marketplace="ozon"
    )

    html = client_logged_in.get("/movement/?q=191+москва").get_data(as_text=True)

    assert matching.number in html
    assert other_city.number not in html
    assert other_box.number not in html


def test_search_with_marketplace_alias_and_city(db, client_logged_in):
    """«озон москва» — слово "озон" понимается как площадка склада
    назначения (Warehouse.marketplace == "ozon"), а не ищется буквально в
    названии (в названии склада этого слова обычно и нет)."""
    ozon_moscow = _make_box_document(
        "PER-MULTI-011", "BOX-OZ-1", "Основной", "Москва", dest_marketplace="ozon"
    )
    wb_moscow = _make_box_document(
        "PER-MULTI-012", "BOX-WB-1", "Основной", "Москва", dest_marketplace="wb"
    )
    ozon_kazan = _make_box_document(
        "PER-MULTI-013", "BOX-OZ-2", "Основной", "Казань", dest_marketplace="ozon"
    )

    html = client_logged_in.get("/movement/?q=озон+москва").get_data(as_text=True)

    assert ozon_moscow.number in html
    assert wb_moscow.number not in html
    assert ozon_kazan.number not in html
