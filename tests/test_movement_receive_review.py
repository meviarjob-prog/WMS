"""Массовая проверка приемки неприятых перемещений на Ozon (кнопка для
роли админ, см. чат: "нужна кнопка для роли админ для проверки всех
непринятых перемещений, провести все совпадающие..."). Переиспользует
автоприемку по API (см. test_movement_receive_via_ozon_api.py) для
точного совпадения; дополнительно: если недостача ровно на один короб —
он возвращается на склад-отправитель, остальное принимается; все прочее
(излишки, неоднозначная недостача, ошибки API) — в список на ручную
проверку, с подсказкой при похожем обратном расхождении в другом
перемещении из этой же проверки (ошибка при отправке — короб попал не в
ту поставку)."""

from datetime import datetime
from unittest.mock import patch

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    MovementCargoPlace,
    MovementDocument,
    MovementLine,
    Nomenclature,
    User,
    Warehouse,
)
from wms.utils.ozon_api import OzonApiError


def _warehouses(suffix):
    sender = Warehouse(code=f"WH-RV{suffix}S", name=f"Основной {suffix}")
    dest = Warehouse(code=f"WH-RV{suffix}D", name=f"Коледино {suffix}", marketplace="ozon")
    db.session.add_all([sender, dest])
    db.session.commit()
    return sender, dest


def _nomenclature(suffix, name="Товар"):
    n = Nomenclature(sku=f"RV-{suffix}", barcode=f"29000000000{suffix:0>2}", name=name, unit="шт")
    db.session.add(n)
    db.session.commit()
    return n


def _make_doc(number, order_number, sender, dest, boxes):
    """boxes — список коробов, каждый — список (nomenclature, qty)."""
    doc = MovementDocument(
        number=number, from_warehouse_id=sender.id, to_warehouse_id=dest.id, status="completed",
        marketplace_request_created_at=datetime.utcnow(), marketplace_request_number=order_number,
        shipped_at=datetime.utcnow(),
    )
    db.session.add(doc)
    db.session.commit()
    box_objs = []
    for i, items in enumerate(boxes):
        box = Box(box_number=f"{number}-BOX{i}", warehouse_id=dest.id, status="open")
        db.session.add(box)
        db.session.commit()
        for nom, qty in items:
            db.session.add(BoxItem(box_id=box.id, nomenclature_id=nom.id, qty=qty))
        db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
        box_objs.append(box)
    db.session.commit()
    return doc, box_objs


def _order(order_number, supply_id):
    return {
        "order_id": supply_id,
        "order_number": order_number,
        "state": "ACCEPTANCE_AT_STORAGE_WAREHOUSE",
        "supplies": [{"supply_id": supply_id, "state": "ACCEPTANCE_AT_STORAGE_WAREHOUSE"}],
    }


def _act_items(items_by_barcode):
    return (
        [
            {"type": "ACCEPTANCE", "barcode": barcode, "offer_id": "", "sku": 1, "name": "",
             "declared_quantity": qty, "approved_quantity": qty, "fact_quantity": qty}
            for barcode, qty in items_by_barcode.items()
        ],
        0,
    )


def test_non_admin_cannot_see_or_run_review(db, client):
    worker = User(username="rv_worker", full_name="Не админ", role="warehouse")
    worker.set_password("pw12345")
    db.session.add(worker)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(worker.id)
        sess["_fresh"] = True

    resp = client.get("/movement/receive-review", follow_redirects=True)
    assert "только администратору" in resp.get_data(as_text=True)

    resp = client.post("/movement/receive-review/run", follow_redirects=True)
    assert "только администратору" in resp.get_data(as_text=True)


@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_full_match_auto_accepted(mock_orders, mock_items, db, client_logged_in):
    sender, dest = _warehouses(1)
    n1 = _nomenclature(1)
    doc, boxes = _make_doc("PER-RV0001", "REQ-RV1", sender, dest, [[(n1, 10)]])
    mock_orders.return_value = [_order("REQ-RV1", 701)]
    mock_items.return_value = _act_items({n1.barcode: 10})

    resp = client_logged_in.get("/movement/receive-review")
    assert "PER-RV0001" in resp.get_data(as_text=True)

    resp = client_logged_in.post("/movement/receive-review/run")
    html = resp.get_data(as_text=True)
    assert "PER-RV0001" in html
    assert "Принято полностью" in html

    db.session.refresh(doc)
    assert doc.received_at is not None
    assert doc.received_qty_snapshot == 10


@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_single_missing_box_returned_and_rest_accepted(mock_orders, mock_items, db, client_logged_in):
    sender, dest = _warehouses(2)
    n1 = _nomenclature(2)
    doc, boxes = _make_doc("PER-RV0002", "REQ-RV2", sender, dest, [[(n1, 10)], [(n1, 4)]])
    mock_orders.return_value = [_order("REQ-RV2", 702)]
    # Принято только 10 (как в первом коробе) — второй короб (4 шт) не доехал.
    mock_items.return_value = _act_items({n1.barcode: 10})

    resp = client_logged_in.post("/movement/receive-review/run")
    html = resp.get_data(as_text=True)
    assert "Короб возвращен" in html
    assert boxes[1].box_number in html

    db.session.refresh(doc)
    assert doc.received_at is not None
    assert doc.received_qty_snapshot == 10
    assert doc.receipt_returned_boxes == boxes[1].box_number
    assert MovementLine.query.filter_by(document_id=doc.id, box_id=boxes[1].id).first() is None
    assert MovementCargoPlace.query.filter_by(document_id=doc.id, box_id=boxes[1].id).first() is None

    returned_box = Box.query.get(boxes[1].id)
    assert returned_box.warehouse_id == sender.id


@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_ambiguous_shortage_goes_to_manual(mock_orders, mock_items, db, client_logged_in):
    sender, dest = _warehouses(3)
    n1 = _nomenclature(3)
    # Один короб на 10 шт, принято 7 — недостача 3 шт не совпадает ни с
    # одним коробом целиком (короб только один, и он не пуст).
    doc, boxes = _make_doc("PER-RV0003", "REQ-RV3", sender, dest, [[(n1, 10)]])
    mock_orders.return_value = [_order("REQ-RV3", 703)]
    mock_items.return_value = _act_items({n1.barcode: 7})

    resp = client_logged_in.post("/movement/receive-review/run")
    html = resp.get_data(as_text=True)
    assert "Требуется ручная проверка" in html
    assert "PER-RV0003" in html
    assert "отправлено 10" in html
    assert "по Ozon принято 7" in html

    db.session.refresh(doc)
    assert doc.received_at is None


@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_surplus_blocks_box_return_heuristic(mock_orders, mock_items, db, client_logged_in):
    sender, dest = _warehouses(4)
    n1 = _nomenclature(4)
    # Второй короб (4 шт) как бы "не доехал", но есть еще и неожиданный
    # излишек по этому же товару — автоматически считать короб недоехавшим
    # нельзя (расхождение может быть ошибкой, а не доехавшим коробом).
    doc, boxes = _make_doc("PER-RV0004", "REQ-RV4", sender, dest, [[(n1, 10)], [(n1, 4)]])
    mock_orders.return_value = [_order("REQ-RV4", 704)]
    mock_items.return_value = _act_items({n1.barcode: 11})  # 10 + 1 неожиданного излишка

    resp = client_logged_in.post("/movement/receive-review/run")
    html = resp.get_data(as_text=True)
    assert "Требуется ручная проверка" in html
    assert "PER-RV0004" in html

    db.session.refresh(doc)
    assert doc.received_at is None
    assert MovementLine.query.filter_by(document_id=doc.id).count() == 2


@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_cross_movement_mixup_hint(mock_orders, mock_items, db, client_logged_in):
    sender, dest = _warehouses(5)
    n1 = _nomenclature(5)
    # Перемещение А: недостача 3 шт, не совпадающая ровно с коробом (значит
    # не авто-возврат короба). Перемещение Б: излишек ровно 3 шт того же
    # товара — похоже, короб перепутали между отправками.
    doc_a, _ = _make_doc("PER-RV0005", "REQ-RV5A", sender, dest, [[(n1, 10)]])
    doc_b, _ = _make_doc("PER-RV0006", "REQ-RV5B", sender, dest, [[(n1, 5)]])

    def orders_side_effect(client_id, api_key, order_number=None, days_back=None):
        if order_number == "REQ-RV5A":
            return [_order("REQ-RV5A", 705)]
        if order_number == "REQ-RV5B":
            return [_order("REQ-RV5B", 706)]
        return []

    def items_side_effect(client_id, api_key, supply_id):
        if supply_id == 705:
            return _act_items({n1.barcode: 7})  # недостача 3
        if supply_id == 706:
            return _act_items({n1.barcode: 8})  # излишек 3
        return [], 0

    mock_orders.side_effect = orders_side_effect
    mock_items.side_effect = items_side_effect

    resp = client_logged_in.post("/movement/receive-review/run")
    html = resp.get_data(as_text=True)
    assert "PER-RV0005" in html and "PER-RV0006" in html
    assert "возможно, короб по ошибке попал не в ту поставку" in html

    db.session.refresh(doc_a)
    db.session.refresh(doc_b)
    assert doc_a.received_at is None
    assert doc_b.received_at is None


@patch("wms.blueprints.movement.fetch_supply_orders")
def test_ozon_api_error_goes_to_manual(mock_orders, db, client_logged_in):
    sender, dest = _warehouses(6)
    n1 = _nomenclature(6)
    doc, _ = _make_doc("PER-RV0007", "REQ-RV7", sender, dest, [[(n1, 10)]])
    mock_orders.side_effect = OzonApiError("Ozon API вернул ошибку 500: boom")

    resp = client_logged_in.post("/movement/receive-review/run")
    html = resp.get_data(as_text=True)
    assert "Требуется ручная проверка" in html
    assert "PER-RV0007" in html
    assert "boom" in html

    db.session.refresh(doc)
    assert doc.received_at is None


def test_pending_list_excludes_wb_and_already_received_and_missing_request_number(db, client_logged_in):
    sender, dest = _warehouses(7)
    wb_dest = Warehouse(code="WH-RV7WB", name="WB склад", marketplace="wb")
    db.session.add(wb_dest)
    db.session.commit()
    n1 = _nomenclature(7)

    ok_doc, _ = _make_doc("PER-RV0008", "REQ-RV8", sender, dest, [[(n1, 1)]])

    no_number_doc, _ = _make_doc("PER-RV0009", "", sender, dest, [[(n1, 1)]])
    no_number_doc.marketplace_request_number = None
    no_number_doc.marketplace_request_created_at = None

    wb_doc, _ = _make_doc("PER-RV0010", "REQ-RV10", sender, wb_dest, [[(n1, 1)]])

    received_doc, _ = _make_doc("PER-RV0011", "REQ-RV11", sender, dest, [[(n1, 1)]])
    received_doc.received_at = datetime.utcnow()
    db.session.commit()

    resp = client_logged_in.get("/movement/receive-review")
    html = resp.get_data(as_text=True)
    assert "PER-RV0008" in html
    assert "PER-RV0009" not in html
    assert "PER-RV0010" not in html
    assert "PER-RV0011" not in html
