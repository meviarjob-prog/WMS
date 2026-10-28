"""Грузоместа Ozon в карточке перемещения: статус каждого грузоместа по API
и наш короб (по штрихкоду ГМ из «Состава ГМ» или по составу). Только
просмотр — ничего не меняет."""

from datetime import datetime
from unittest.mock import patch

from wms.extensions import db
from wms.models import Box, BoxItem, MovementCargoPlace, MovementDocument, MovementLine, Nomenclature, Warehouse


def _setup():
    sender = Warehouse(code="WH-CGS", name="Основной")
    dest = Warehouse(code="WH-CGD", name="Казань", marketplace="ozon")
    hat = Nomenclature(sku="CG-1", barcode="4600000000011", name="Шапка", unit="шт")
    scarf = Nomenclature(sku="CG-2", barcode="4600000000028", name="Шарф", unit="шт")
    db.session.add_all([sender, dest, hat, scarf])
    db.session.commit()
    doc = MovementDocument(
        number="PER-CG1", from_warehouse_id=sender.id, to_warehouse_id=dest.id, status="completed",
        marketplace_request_number="2000070000001", marketplace_request_created_at=datetime.utcnow(),
        shipped_at=datetime.utcnow(),
    )
    db.session.add(doc)
    db.session.commit()
    boxes = []
    for number, items in (("BOX-CG1", {hat: 10}), ("BOX-CG2", {scarf: 4}), ("BOX-CG3", {hat: 3, scarf: 1})):
        box = Box(box_number=number, warehouse_id=dest.id, status="open")
        db.session.add(box)
        db.session.commit()
        for nom, qty in items.items():
            db.session.add(BoxItem(box_id=box.id, nomenclature_id=nom.id, qty=qty))
        db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
        boxes.append(box)
    db.session.add(MovementCargoPlace(document_id=doc.id, box_id=boxes[0].id, box_number="BOX-CG1",
                                      cargo_barcode="OZN0001", items_json="[]"))
    db.session.commit()
    return doc, boxes, hat, scarf


@patch("wms.blueprints.movement.fetch_bundle_items")
@patch("wms.blueprints.movement.fetch_cargo_statuses")
@patch("wms.blueprints.movement.fetch_supply_cargo_places")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_cargo_table_matches_boxes_and_marks_not_delivered(mock_orders, mock_places, mock_statuses, mock_bundle,
                                                           db, client_logged_in):
    doc, boxes, hat, scarf = _setup()
    mock_orders.return_value = [{"order_number": "2000070000001", "supplies": [{"supply_id": 900}]}]
    mock_places.return_value = {900: [
        {"cargo_id": 1, "barcode": "ozn0001", "bundle_id": "b1"},     # по штрихкоду ГМ
        {"cargo_id": 2, "barcode": "OZN0002", "bundle_id": "b2"},     # по составу — шарфы
    ]}
    mock_statuses.return_value = {1: {"status": "PROCESSED"}, 2: {"status": "NOT_DELIVERED"}}
    mock_bundle.return_value = [{"barcode": scarf.barcode, "offer_id": "", "sku": 1, "quantity": 4}]

    html = client_logged_in.get(f"/movement/{doc.id}/ozon-cargoes").get_data(as_text=True)
    assert "Не принято Ozon: 1" in html
    assert "по штрихкоду ГМ" in html and "по составу" in html
    danger_row = html[html.index("table-danger"):]
    assert "OZN0002" in danger_row[:400] and "Не сдано" in danger_row[:400] and "BOX-CG2" in danger_row[:400]
    # Третий короб в грузоместах Ozon не нашелся.
    assert "без пары среди грузомест Ozon" in html and "BOX-CG3" in html
    mock_bundle.assert_called_once()        # состав запрашиваем только для несопоставленных
    # Только просмотр.
    db.session.refresh(doc)
    assert doc.received_at is None and doc.lines.count() == 3


@patch("wms.blueprints.movement.fetch_supply_orders")
def test_cargo_table_reports_ozon_error(mock_orders, db, client_logged_in):
    from wms.utils.ozon_api import OzonApiError

    doc, *_ = _setup()
    mock_orders.side_effect = OzonApiError("нет связи")
    html = client_logged_in.get(f"/movement/{doc.id}/ozon-cargoes").get_data(as_text=True)
    assert "Не удалось получить грузоместа из Ozon: нет связи" in html


def test_detail_page_has_cargo_button(db, client_logged_in):
    doc, *_ = _setup()
    html = client_logged_in.get(f"/movement/{doc.id}").get_data(as_text=True)
    assert f'data-ozon-cargoes="/movement/{doc.id}/ozon-cargoes"' in html
