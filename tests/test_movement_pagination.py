"""Список перемещений выводится страницами по 50 документов."""

from datetime import datetime, timedelta

from wms.extensions import db
from wms.models import MovementDocument, Warehouse


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


def test_movement_list_is_paginated_by_fifty(db, client_logged_in):
    _create_documents(51)

    first_page = client_logged_in.get("/movement/").get_data(as_text=True)
    assert first_page.count('class="movement-number text-decoration-none"') == 50
    assert "PER-PAGE-051" in first_page
    assert "PER-PAGE-002" in first_page
    assert "PER-PAGE-001" not in first_page
    assert "Страница 1 из 2" in first_page
    assert "всего перемещений: 51" in first_page
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
    assert "movement-list-card" in html
    assert "movement-list-item" in html
