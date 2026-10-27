"""Автоприёмка перемещения по данным Ozon API (movement.receive_via_ozon_api,
см. чат: "нужно реализовать загрузку приемки из озон через апи"). API
Ozon отдаёт фактически принятое количество только суммарно по товару за
всю поставку — без разбивки по коробам/грузоместам (сверено по
официальному Swagger-спеку Ozon, присланному пользователем). Поэтому
автоприёмка срабатывает, только если сошлось ПОЛНОСТЬЮ по каждому
товару — иначе ничего не меняется, нужно принимать вручную или актом
файлом (см. test_movement_receive_act.py)."""

from datetime import datetime
from unittest.mock import patch

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    MovementReceiptDiscrepancy,
    Nomenclature,
    OzonArticleMapping,
    ShipmentPlan,
    ShipmentPlanLine,
    Warehouse,
)
from wms.utils.ozon_api import OzonApiError


def _setup(order_number="REQ-1"):
    sender = Warehouse(code="WH-OA1", name="Основной")
    dest = Warehouse(code="WH-OA2", name="Коледино", marketplace="ozon")
    db.session.add_all([sender, dest])
    n1 = Nomenclature(sku="OA-1", barcode="3000000000011", name="Шапка", unit="шт")
    n2 = Nomenclature(sku="OA-2", barcode="3000000000028", name="Свитер", unit="шт")
    db.session.add_all([n1, n2])
    db.session.commit()
    plan = ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    db.session.commit()
    db.session.add_all([
        ShipmentPlanLine(plan_id=plan.id, warehouse_id=dest.id, nomenclature_id=n1.id, barcode=n1.barcode, planned_qty=100),
        ShipmentPlanLine(plan_id=plan.id, warehouse_id=dest.id, nomenclature_id=n2.id, barcode=n2.barcode, planned_qty=100),
    ])
    box = Box(box_number="BOX-OA1", warehouse_id=dest.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add_all([
        BoxItem(box_id=box.id, nomenclature_id=n1.id, qty=10),
        BoxItem(box_id=box.id, nomenclature_id=n2.id, qty=5),
    ])
    doc = MovementDocument(
        number="PER-OA0001", from_warehouse_id=sender.id, to_warehouse_id=dest.id, status="completed",
        marketplace_request_created_at=datetime.utcnow(), marketplace_request_number=order_number,
        shipped_at=datetime.utcnow(),
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
    db.session.commit()
    return doc, sender, dest, n1, n2, box


def _order(order_number, supply_id=555):
    return {
        "order_id": 1,
        "order_number": order_number,
        "state": "ACCEPTANCE_AT_STORAGE_WAREHOUSE",
        "supplies": [{"supply_id": supply_id, "state": "ACCEPTANCE_AT_STORAGE_WAREHOUSE"}],
    }


def _act_response(items_by_barcode, act_type="ACCEPTANCE", unidentified=0):
    return {
        "supply_id": 555,
        "supply_acts": [
            {
                "act_id": 1,
                "type": act_type,
                "unidentified_quantity": unidentified,
                "items": [
                    {
                        "declared_quantity": qty,
                        "approved_quantity": qty,
                        "fact_quantity": qty,
                        "sku_info": {"barcode": barcode, "offer_id": "", "sku": 1},
                    }
                    for barcode, qty in items_by_barcode.items()
                ],
            }
        ],
    }


@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_auto_accept_when_fully_matched(mock_orders, mock_items, db, client_logged_in):
    doc, sender, dest, n1, n2, box = _setup()
    mock_orders.return_value = [_order("REQ-1")]
    mock_items.return_value = (
        [
            {"type": "ACCEPTANCE", "barcode": n1.barcode, "offer_id": "", "sku": 1, "name": "",
             "declared_quantity": 10, "approved_quantity": 10, "fact_quantity": 10},
            {"type": "ACCEPTANCE", "barcode": n2.barcode, "offer_id": "", "sku": 1, "name": "",
             "declared_quantity": 5, "approved_quantity": 5, "fact_quantity": 5},
        ],
        0,
    )

    resp = client_logged_in.post(f"/movement/{doc.id}/receive/ozon-api", follow_redirects=True)

    assert resp.status_code == 200
    assert "совпало полностью" in resp.get_data(as_text=True)
    db.session.refresh(doc)
    assert doc.received_at is not None
    assert doc.received_qty_snapshot == 15
    assert MovementReceiptDiscrepancy.query.filter_by(document_id=doc.id).count() == 0
    mock_items.assert_called_once_with(None, None, 555)


@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_mismatch_blocks_auto_accept(mock_orders, mock_items, db, client_logged_in):
    doc, sender, dest, n1, n2, box = _setup()
    mock_orders.return_value = [_order("REQ-1")]
    mock_items.return_value = (
        [
            {"type": "ACCEPTANCE", "barcode": n1.barcode, "offer_id": "", "sku": 1, "name": "",
             "declared_quantity": 10, "approved_quantity": 7, "fact_quantity": 7},
            {"type": "ACCEPTANCE", "barcode": n2.barcode, "offer_id": "", "sku": 1, "name": "",
             "declared_quantity": 5, "approved_quantity": 5, "fact_quantity": 5},
        ],
        0,
    )

    resp = client_logged_in.post(f"/movement/{doc.id}/receive/ozon-api", follow_redirects=True)

    html = resp.get_data(as_text=True)
    assert "показывает расхождение" in html
    assert "отправлено 10" in html
    assert "по Ozon принято 7" in html
    db.session.refresh(doc)
    assert doc.received_at is None


@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_unidentified_surplus_blocks_auto_accept(mock_orders, mock_items, db, client_logged_in):
    doc, sender, dest, n1, n2, box = _setup()
    mock_orders.return_value = [_order("REQ-1")]
    mock_items.return_value = ([], 3)  # неопознанные излишки

    resp = client_logged_in.post(f"/movement/{doc.id}/receive/ozon-api", follow_redirects=True)

    assert "неопознанные излишки" in resp.get_data(as_text=True)
    db.session.refresh(doc)
    assert doc.received_at is None


def test_missing_marketplace_request_number_blocks_auto_accept(db, client_logged_in):
    doc, sender, dest, n1, n2, box = _setup(order_number="")
    # Без номера заявки приемка вообще недоступна (общий precheck) — но
    # если он есть, а запрос в Ozon его не находит, должна быть отдельная
    # понятная ошибка (см. test_order_not_found_in_ozon_blocks_auto_accept).
    resp = client_logged_in.post(f"/movement/{doc.id}/receive/ozon-api", follow_redirects=True)
    assert "Сначала внесите номер заявки" in resp.get_data(as_text=True)
    db.session.refresh(doc)
    assert doc.received_at is None


@patch("wms.blueprints.movement.fetch_supply_orders")
def test_order_not_found_in_ozon_blocks_auto_accept(mock_orders, db, client_logged_in):
    doc, sender, dest, n1, n2, box = _setup()
    mock_orders.return_value = []

    resp = client_logged_in.post(f"/movement/{doc.id}/receive/ozon-api", follow_redirects=True)

    assert "не найдена в Ozon API" in resp.get_data(as_text=True)
    db.session.refresh(doc)
    assert doc.received_at is None


@patch("wms.blueprints.movement.fetch_supply_orders")
def test_ozon_api_error_blocks_auto_accept(mock_orders, db, client_logged_in):
    doc, sender, dest, n1, n2, box = _setup()
    mock_orders.side_effect = OzonApiError("Ozon API вернул ошибку 500: boom")

    resp = client_logged_in.post(f"/movement/{doc.id}/receive/ozon-api", follow_redirects=True)

    assert "Не удалось свериться с Ozon API" in resp.get_data(as_text=True)
    db.session.refresh(doc)
    assert doc.received_at is None


@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_matches_item_by_ozon_article_mapping_when_barcode_differs(mock_orders, mock_items, db, client_logged_in):
    """Ozon может отдать товар по своему offer_id, у которого в акте
    штрихкод не совпадает с нашим (или пуст) — сопоставление через
    OzonArticleMapping, как и для файла акта (см. _nomenclature_for_act_item)."""
    doc, sender, dest, n1, n2, box = _setup()
    db.session.add(OzonArticleMapping(article="OZN-OFFER-1", barcode=n1.barcode))
    db.session.commit()
    mock_orders.return_value = [_order("REQ-1")]
    mock_items.return_value = (
        [
            {"type": "ACCEPTANCE", "barcode": "", "offer_id": "OZN-OFFER-1", "sku": 1, "name": "",
             "declared_quantity": 10, "approved_quantity": 10, "fact_quantity": 10},
            {"type": "ACCEPTANCE", "barcode": n2.barcode, "offer_id": "", "sku": 1, "name": "",
             "declared_quantity": 5, "approved_quantity": 5, "fact_quantity": 5},
        ],
        0,
    )

    resp = client_logged_in.post(f"/movement/{doc.id}/receive/ozon-api", follow_redirects=True)

    assert "совпало полностью" in resp.get_data(as_text=True)
    db.session.refresh(doc)
    assert doc.received_at is not None


def test_not_available_for_non_ozon_destination(db, client_logged_in):
    sender = Warehouse(code="WH-OA3", name="Основной")
    dest = Warehouse(code="WH-OA4", name="Невинномысск", marketplace="wb")
    db.session.add_all([sender, dest])
    n1 = Nomenclature(sku="OA-3", barcode="3000000000035", name="Товар", unit="шт")
    db.session.add(n1)
    db.session.commit()
    box = Box(box_number="BOX-OA2", warehouse_id=dest.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=n1.id, qty=3))
    doc = MovementDocument(
        number="PER-OA0002", from_warehouse_id=sender.id, to_warehouse_id=dest.id, status="completed",
        marketplace_request_created_at=datetime.utcnow(), marketplace_request_number="REQ-WB",
        shipped_at=datetime.utcnow(),
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
    db.session.commit()

    resp = client_logged_in.post(f"/movement/{doc.id}/receive/ozon-api", follow_redirects=True)

    assert "доступна только для перемещений на Ozon" in resp.get_data(as_text=True)
