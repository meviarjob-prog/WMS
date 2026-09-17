"""Кнопки выгрузки для маркетплейсов (Ozon/WB) на странице перемещения
(movement/detail.html) должны быть доступны сразу после того, как в
перемещение добавлены короба ("сформировано"), а не только после его
завершения ("Собрано") — сам экспорт (marketplace_export.py)
не смотрит на doc.status, ему достаточно строк перемещения."""

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, MovementLine, Nomenclature, Warehouse


def _make_movement(marketplace, status):
    sender = Warehouse(code=f"WH-MPB-{marketplace}-1", name="Склад-отправитель")
    dest = Warehouse(
        code=f"WH-MPB-{marketplace}-2",
        name=f"{marketplace.upper()}: склад",
        marketplace=marketplace,
    )
    db.session.add_all([sender, dest])
    db.session.commit()

    item = Nomenclature(sku=f"SKU-MPB-{marketplace}", barcode=f"9990{marketplace}0001", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    box = Box(box_number=f"BOX-MPB-{marketplace}", warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=3))

    doc = MovementDocument(
        number=f"PER-MPB-{marketplace}", from_warehouse_id=sender.id, to_warehouse_id=dest.id, status=status
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
    db.session.commit()
    return doc


def test_ozon_buttons_visible_on_draft_movement_with_boxes(db, client_logged_in):
    doc = _make_movement("ozon", "draft")

    html = client_logged_in.get(f"/movement/{doc.id}").get_data(as_text=True)

    assert "Состав ГМ для Ozon" in html
    assert "Заявка на поставку (Ozon)" in html


def test_wb_button_visible_on_draft_movement_with_boxes(db, client_logged_in):
    doc = _make_movement("wb", "draft")

    html = client_logged_in.get(f"/movement/{doc.id}").get_data(as_text=True)

    assert "Состав для WB" in html


def test_marketplace_buttons_hidden_when_movement_has_no_boxes_yet(db, client_logged_in):
    sender = Warehouse(code="WH-MPB-empty-1", name="Склад-отправитель")
    dest = Warehouse(code="WH-MPB-empty-2", name="ОЗОН: пусто", marketplace="ozon")
    db.session.add_all([sender, dest])
    db.session.commit()

    doc = MovementDocument(number="PER-MPB-EMPTY", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()

    html = client_logged_in.get(f"/movement/{doc.id}").get_data(as_text=True)

    assert "Состав ГМ для Ozon" not in html
    assert "Заявка на поставку (Ozon)" not in html
