"""Список приемок (receiving.list_documents) — подсветка строк, у которых
есть возврат поставщику или расхождение с накладной, колонка "Кол-во"
(суммарное количество по строкам документа) и колонка "Возвраты"
(кол-во SupplierReturn по документу). См. также timestamps
recounting_started_at/sorting_started_at на детальной странице."""

from wms.extensions import db
from wms.models import (
    Nomenclature,
    ReceivingDocument,
    ReceivingLine,
    SupplierReturn,
    Warehouse,
)


def _make_warehouse(code="WH-RLH"):
    wh = Warehouse(code=code, name="Тест склад подсветки")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(suffix):
    item = Nomenclature(sku=f"SKU-RLH{suffix}", barcode=f"77707000{suffix}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def test_document_with_return_is_highlighted_and_shows_return_count(db, client_logged_in):
    wh = _make_warehouse("1")
    item = _make_item("1")
    doc = ReceivingDocument(number="RLH-0001", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        SupplierReturn(receiving_document_id=doc.id, warehouse_id=wh.id, nomenclature_id=item.id, qty=2)
    )
    db.session.commit()

    html = client_logged_in.get("/receiving/").get_data(as_text=True)

    assert "table-danger" in html
    assert "RLH-0001" in html
    assert "mobile-compact-list-page" in html
    assert "receiving-list-card" in html
    assert "receiving-list-item" in html


def test_document_with_mismatch_is_highlighted_without_return(db, client_logged_in):
    wh = _make_warehouse("2")
    item = _make_item("2")
    doc = ReceivingDocument(number="RLH-0002", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=8, expected_qty=10)
    )
    db.session.commit()

    html = client_logged_in.get("/receiving/").get_data(as_text=True)

    assert "table-warning" in html


def test_document_without_return_or_mismatch_is_not_highlighted(db, client_logged_in):
    wh = _make_warehouse("3")
    item = _make_item("3")
    doc = ReceivingDocument(number="RLH-0003", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=5, expected_qty=5))
    db.session.commit()

    html = client_logged_in.get("/receiving/").get_data(as_text=True)

    assert "table-danger" not in html
    assert "table-warning" not in html


def test_list_shows_total_qty_and_returns_count(db, client_logged_in):
    wh = _make_warehouse("4")
    item1 = _make_item("4a")
    item2 = _make_item("4b")
    doc = ReceivingDocument(number="RLH-0004", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add_all(
        [
            ReceivingLine(document_id=doc.id, nomenclature_id=item1.id, qty=3),
            ReceivingLine(document_id=doc.id, nomenclature_id=item2.id, qty=4),
        ]
    )
    db.session.add(
        SupplierReturn(receiving_document_id=doc.id, warehouse_id=wh.id, nomenclature_id=item1.id, qty=1)
    )
    db.session.add(
        SupplierReturn(receiving_document_id=doc.id, warehouse_id=wh.id, nomenclature_id=item2.id, qty=1)
    )
    db.session.commit()

    html = client_logged_in.get("/receiving/").get_data(as_text=True)

    assert ">7<" in html
    assert 'bg-danger">2<' in html


def test_detail_page_shows_status_change_dates(db, client_logged_in):
    """Даты перехода статусов видны в подсказке к плашке "Пересчет" (см.
    tests/test_receiving_status_chips.py — там же проверка самих плашек)."""
    wh = _make_warehouse("5")
    item = _make_item("5")
    doc = ReceivingDocument(number="RLH-0005", warehouse_id=wh.id, invoice_file_name="накладная.xlsx")
    db.session.add(doc)
    db.session.commit()
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=5, expected_qty=5)
    db.session.add(line)
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/send-to-recount")
    html = client_logged_in.get(f"/receiving/{doc.id}").get_data(as_text=True)

    assert "Пересчет:" in html
    doc = ReceivingDocument.query.get(doc.id)
    assert doc.recounting_started_at is not None
