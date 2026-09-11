"""Поле поиска товара при приемке совмещено со сканированием штрихкода
(см. wms/static/js/app.js: initNomenclatureAutocomplete) — отдельного поля
"Сканировать штрихкод товара" с автодобавлением qty=1 больше нет, вместо
него один автокомплит-инпут: скан по Enter с единственным совпадением
подставляет товар автоматически, ручной поиск с несколькими совпадениями
ведет себя как раньше (просто список вариантов)."""

from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, ReceivingDocument, ReceivingLine, Warehouse


def _make_warehouse(code="WH-MERGE"):
    wh = Warehouse(code=code, name="Тест склад слияния")
    db.session.add(wh)
    db.session.commit()
    return wh


def test_detail_page_has_single_merged_add_item_field(db, client_logged_in):
    wh = _make_warehouse()
    doc = ReceivingDocument(number="MERGE-DOC-1", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()

    html = client_logged_in.get(f"/receiving/{doc.id}").get_data(as_text=True)

    assert 'id="receivingItemInput"' in html
    assert "Сканировать штрихкод товара" not in html
    assert "scanInput" not in html


def test_add_line_still_works_through_merged_form(db, client_logged_in):
    """Форма добавления (без активного короба) шлет на тот же receiving.add_line,
    что и раньше — сам бэкенд не менялся, менялся только UI поиска/скана."""
    wh = _make_warehouse("WH-MERGE-2")
    item = Nomenclature(sku="MERGE-1", barcode="8880001112223", name="Товар слияния", unit="шт")
    db.session.add(item)
    doc = ReceivingDocument(number="MERGE-DOC-2", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()

    client_logged_in.post(
        f"/receiving/{doc.id}/lines/add", data={"nomenclature_id": item.id, "qty": 3}
    )

    line = ReceivingLine.query.filter_by(document_id=doc.id, nomenclature_id=item.id).first()
    assert line.qty == 3


def test_add_line_to_box_still_works_through_merged_form_with_active_box(db, client_logged_in):
    wh = _make_warehouse("WH-MERGE-3")
    item = Nomenclature(sku="MERGE-2", barcode="8880002223334", name="Товар слияния 2", unit="шт")
    box = Box(box_number="BOX-MERGE-1", warehouse_id=wh.id, status="open")
    db.session.add_all([item, box])
    doc = ReceivingDocument(number="MERGE-DOC-3", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()

    html = client_logged_in.get(f"/receiving/{doc.id}?box={box.id}").get_data(as_text=True)
    assert 'id="receivingItemInput"' in html

    client_logged_in.post(
        f"/receiving/{doc.id}/boxes/{box.id}/lines/add", data={"nomenclature_id": item.id, "qty": 2}
    )

    box_item = BoxItem.query.filter_by(box_id=box.id, nomenclature_id=item.id).first()
    assert box_item.qty == 2
