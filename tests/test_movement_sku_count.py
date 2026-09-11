"""Колонка "Кол-во SKU" в списке перемещений (см. MovementDocument.sku_count,
movement/list.html) — количество РАЗНЫХ товаров во всех коробах документа,
не путать с количеством коробов. Показывается только в десктопной таблице
(мобильный список карточек этой колонки не содержит)."""

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, MovementLine, Nomenclature, Warehouse


def _make_warehouses():
    sender = Warehouse(code="WH-SKU-A", name="Склад-отправитель")
    dest = Warehouse(code="WH-SKU-B", name="ОЗОН: Тест")
    db.session.add_all([sender, dest])
    db.session.commit()
    return sender, dest


def test_sku_count_is_zero_for_empty_document(db):
    sender, dest = _make_warehouses()
    doc = MovementDocument(number="PER-SKU-1", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()

    assert doc.sku_count() == 0


def test_sku_count_deduplicates_same_item_across_boxes(db):
    """Один и тот же товар в двух разных коробах — это все равно 1 SKU."""
    sender, dest = _make_warehouses()
    item = Nomenclature(sku="SKU-DUP", barcode="4445556667778", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    doc = MovementDocument(number="PER-SKU-2", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()

    for i in range(2):
        box = Box(box_number=f"BOX-SKU-2{i}", warehouse_id=sender.id, status="open")
        db.session.add(box)
        db.session.commit()
        db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=5))
        db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
    db.session.commit()

    assert doc.sku_count() == 1


def test_sku_count_counts_distinct_items_across_boxes(db):
    sender, dest = _make_warehouses()
    item1 = Nomenclature(sku="SKU-A", barcode="5556667778889", name="Товар А", unit="шт")
    item2 = Nomenclature(sku="SKU-B", barcode="6667778889990", name="Товар Б", unit="шт")
    db.session.add_all([item1, item2])
    db.session.commit()

    doc = MovementDocument(number="PER-SKU-3", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()

    box1 = Box(box_number="BOX-SKU-30", warehouse_id=sender.id, status="open")
    box2 = Box(box_number="BOX-SKU-31", warehouse_id=sender.id, status="open")
    db.session.add_all([box1, box2])
    db.session.commit()
    db.session.add(BoxItem(box_id=box1.id, nomenclature_id=item1.id, qty=2))
    db.session.add(BoxItem(box_id=box2.id, nomenclature_id=item2.id, qty=3))
    db.session.add(MovementLine(document_id=doc.id, box_id=box1.id, from_warehouse_id=sender.id))
    db.session.add(MovementLine(document_id=doc.id, box_id=box2.id, from_warehouse_id=sender.id))
    db.session.commit()

    assert doc.sku_count() == 2


def test_movement_list_desktop_table_shows_sku_count_column(db, client_logged_in):
    sender, dest = _make_warehouses()
    item = Nomenclature(sku="SKU-LIST", barcode="7778889990001", name="Товар для списка", unit="шт")
    db.session.add(item)
    db.session.commit()

    doc = MovementDocument(number="PER-SKU-4", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()
    box = Box(box_number="BOX-SKU-40", warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=1))
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
    db.session.commit()

    html = client_logged_in.get("/movement/").get_data(as_text=True)

    assert "Кол-во SKU" in html
