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

from datetime import date, datetime, timedelta
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
from wms.blueprints.movement import (
    RECEIVE_REVIEW_BATCH_LIMIT,
    RECEIVE_REVIEW_CALL_MAX_RETRIES,
    RECEIVE_REVIEW_CALL_TIMEOUT,
    RECEIVE_REVIEW_SLOT_GRACE_DAYS,
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
    doc, boxes = _make_doc("PER-RV0001", "20000001", sender, dest, [[(n1, 10)]])
    mock_orders.return_value = [_order("20000001", 701)]
    mock_items.return_value = _act_items({n1.barcode: 10})

    resp = client_logged_in.get("/movement/receive-review")
    assert "PER-RV0001" in resp.get_data(as_text=True)

    resp = client_logged_in.post("/movement/receive-review/run", data={"doc_ids": [str(doc.id)]})
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
    doc, boxes = _make_doc("PER-RV0002", "20000002", sender, dest, [[(n1, 10)], [(n1, 4)]])
    mock_orders.return_value = [_order("20000002", 702)]
    # Принято только 10 (как в первом коробе) — второй короб (4 шт) не доехал.
    mock_items.return_value = _act_items({n1.barcode: 10})

    resp = client_logged_in.post("/movement/receive-review/run", data={"doc_ids": [str(doc.id)]})
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
    doc, boxes = _make_doc("PER-RV0003", "20000003", sender, dest, [[(n1, 10)]])
    mock_orders.return_value = [_order("20000003", 703)]
    mock_items.return_value = _act_items({n1.barcode: 7})

    resp = client_logged_in.post("/movement/receive-review/run", data={"doc_ids": [str(doc.id)]})
    html = resp.get_data(as_text=True)
    assert "Требуется ручная проверка" in html
    assert "PER-RV0003" in html
    assert '<td class="text-end">10</td>' in html
    assert '<td class="text-end">7</td>' in html

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
    doc, boxes = _make_doc("PER-RV0004", "20000004", sender, dest, [[(n1, 10)], [(n1, 4)]])
    mock_orders.return_value = [_order("20000004", 704)]
    mock_items.return_value = _act_items({n1.barcode: 11})  # 10 + 1 неожиданного излишка

    resp = client_logged_in.post("/movement/receive-review/run", data={"doc_ids": [str(doc.id)]})
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
    doc_a, _ = _make_doc("PER-RV0005", "20000005", sender, dest, [[(n1, 10)]])
    doc_b, _ = _make_doc("PER-RV0006", "20000006", sender, dest, [[(n1, 5)]])

    def orders_side_effect(client_id, api_key, order_number=None, days_back=None, **kwargs):
        if order_number == "20000005":
            return [_order("20000005", 705)]
        if order_number == "20000006":
            return [_order("20000006", 706)]
        return []

    def items_side_effect(client_id, api_key, supply_id, **kwargs):
        if supply_id == 705:
            return _act_items({n1.barcode: 7})  # недостача 3
        if supply_id == 706:
            return _act_items({n1.barcode: 8})  # излишек 3
        return [], 0

    mock_orders.side_effect = orders_side_effect
    mock_items.side_effect = items_side_effect

    resp = client_logged_in.post(
        "/movement/receive-review/run", data={"doc_ids": [str(doc_a.id), str(doc_b.id)]}
    )
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
    doc, _ = _make_doc("PER-RV0007", "20000007", sender, dest, [[(n1, 10)]])
    mock_orders.side_effect = OzonApiError("Ozon API вернул ошибку 500: boom")

    resp = client_logged_in.post("/movement/receive-review/run", data={"doc_ids": [str(doc.id)]})
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

    ok_doc, _ = _make_doc("PER-RV0008", "20000008", sender, dest, [[(n1, 1)]])

    no_number_doc, _ = _make_doc("PER-RV0009", "", sender, dest, [[(n1, 1)]])
    no_number_doc.marketplace_request_number = None
    no_number_doc.marketplace_request_created_at = None

    wb_doc, _ = _make_doc("PER-RV0010", "20000010", sender, wb_dest, [[(n1, 1)]])

    received_doc, _ = _make_doc("PER-RV0011", "20000011", sender, dest, [[(n1, 1)]])
    received_doc.received_at = datetime.utcnow()
    db.session.commit()

    resp = client_logged_in.get("/movement/receive-review")
    html = resp.get_data(as_text=True)
    assert "PER-RV0008" in html
    assert "PER-RV0009" not in html
    assert "PER-RV0010" not in html
    assert "PER-RV0011" not in html


def test_pending_list_excludes_non_numeric_request_number_and_recent_slot(db, client_logged_in):
    """Чат: "исключить из проверки перемещения с датами слотов не
    наступивших и также перемещения без числовых номеров заявки", позже
    — "текущий день минус 2 дня на приемку". Нечисловой номер (например
    "fbs" — попадает в это поле не как настоящий номер поставки Ozon,
    который всегда числовой) ведет только к бесполезному обращению к
    Ozon API; дата слота в будущем или совсем недавняя (меньше
    RECEIVE_REVIEW_SLOT_GRACE_DAYS дней назад) означает, что Ozon еще не
    успел обработать приемку поставки."""
    sender, dest = _warehouses(16)
    n1 = _nomenclature(16)
    today = date.today()

    fbs_doc, _ = _make_doc("PER-RV0016A", "fbs", sender, dest, [[(n1, 1)]])

    future_slot_doc, _ = _make_doc("PER-RV0016B", "20000020", sender, dest, [[(n1, 1)]])
    future_slot_doc.delivery_slot_date = today + timedelta(days=1)

    recent_slot_doc, _ = _make_doc("PER-RV0016C", "20000021", sender, dest, [[(n1, 1)]])
    recent_slot_doc.delivery_slot_date = today - timedelta(days=RECEIVE_REVIEW_SLOT_GRACE_DAYS - 1)

    today_slot_doc, _ = _make_doc("PER-RV0016D", "20000022", sender, dest, [[(n1, 1)]])
    today_slot_doc.delivery_slot_date = today

    at_cutoff_doc, _ = _make_doc("PER-RV0016F", "20000024", sender, dest, [[(n1, 1)]])
    at_cutoff_doc.delivery_slot_date = today - timedelta(days=RECEIVE_REVIEW_SLOT_GRACE_DAYS)

    no_slot_doc, _ = _make_doc("PER-RV0016E", "20000023", sender, dest, [[(n1, 1)]])
    db.session.commit()

    resp = client_logged_in.get("/movement/receive-review")
    html = resp.get_data(as_text=True)
    assert "PER-RV0016A" not in html
    assert "PER-RV0016B" not in html
    assert "PER-RV0016C" not in html
    assert "PER-RV0016D" not in html
    assert "PER-RV0016E" in html
    assert "PER-RV0016F" in html


@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_batch_limit_caps_processing_and_continue_button_finishes_rest(mock_orders, mock_items, db, client_logged_in):
    """При большом числе накопившихся документов проверка всех сразу
    упиралась в таймаут реверс-прокси (живые запросы к Ozon API на
    каждый документ) — теперь за один вызов проверяется не более
    RECEIVE_REVIEW_BATCH_LIMIT, а остальные ждут повторного нажатия
    кнопки "Продолжить проверку"."""
    sender, dest = _warehouses(12)
    n1 = _nomenclature(12)
    total = RECEIVE_REVIEW_BATCH_LIMIT + 2
    docs = [
        _make_doc(f"PER-RVB{i:03d}", f"2000009{i:03d}", sender, dest, [[(n1, 1)]])[0] for i in range(total)
    ]

    def orders_side_effect(client_id, api_key, order_number=None, days_back=None, **kwargs):
        idx = int(order_number[-3:])
        return [_order(order_number, 9000 + idx)]

    mock_orders.side_effect = orders_side_effect
    mock_items.return_value = _act_items({n1.barcode: 1})

    doc_ids = [str(d.id) for d in docs]
    resp = client_logged_in.post("/movement/receive-review/run", data={"doc_ids": doc_ids})
    html = resp.get_data(as_text=True)
    assert "Продолжить проверку" in html
    assert "еще не проверено: 2" in html

    received = sum(1 for d in docs if MovementDocument.query.get(d.id).received_at is not None)
    assert received == RECEIVE_REVIEW_BATCH_LIMIT

    # Повторная отправка той же (исходной) выбранной группы — уже принятые
    # документы отфильтровываются как неожидающие, обрабатываются только
    # оставшиеся 2 (как и по скрытым полям кнопки "Продолжить проверку").
    resp2 = client_logged_in.post("/movement/receive-review/run", data={"doc_ids": doc_ids})
    html2 = resp2.get_data(as_text=True)
    assert "Продолжить проверку" not in html2

    for d in docs:
        db.session.refresh(d)
        assert d.received_at is not None


def test_no_selection_shows_warning(db, client_logged_in):
    resp = client_logged_in.post("/movement/receive-review/run", follow_redirects=True)
    assert "Выберите хотя бы одно перемещение" in resp.get_data(as_text=True)


@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_unselected_documents_are_not_processed(mock_orders, mock_items, db, client_logged_in):
    sender, dest = _warehouses(13)
    n1 = _nomenclature(13)
    doc_a, _ = _make_doc("PER-RV0013A", "20000013", sender, dest, [[(n1, 1)]])
    doc_b, _ = _make_doc("PER-RV0013B", "20000014", sender, dest, [[(n1, 1)]])
    mock_orders.return_value = [_order("20000013", 713)]
    mock_items.return_value = _act_items({n1.barcode: 1})

    resp = client_logged_in.post("/movement/receive-review/run", data={"doc_ids": [str(doc_a.id)]})
    html = resp.get_data(as_text=True)
    assert "PER-RV0013A" in html
    assert "PER-RV0013B" not in html
    mock_orders.assert_called_once()

    db.session.refresh(doc_a)
    db.session.refresh(doc_b)
    assert doc_a.received_at is not None
    assert doc_b.received_at is None

    resp = client_logged_in.get("/movement/receive-review")
    assert "PER-RV0013B" in resp.get_data(as_text=True)


@patch("wms.blueprints.movement.time.monotonic")
@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_time_budget_stops_batch_even_within_count_limit(mock_orders, mock_items, mock_monotonic, db, client_logged_in):
    """504 повторялся даже при выборе всего 8 документов (меньше
    RECEIVE_REVIEW_BATCH_LIMIT) — обычные таймаут/ретраи Ozon API дают
    больше 5 минут на один зависший вызов, и подсчет документов тут не
    спасает. Проверяем, что при исчерпании RECEIVE_REVIEW_TIME_BUDGET
    обработка пачки останавливается независимо от количества выбранных."""
    sender, dest = _warehouses(14)
    n1 = _nomenclature(14)
    doc1, _ = _make_doc("PER-RV0014A", "20000015", sender, dest, [[(n1, 1)]])
    doc2, _ = _make_doc("PER-RV0014B", "20000016", sender, dest, [[(n1, 1)]])
    doc3, _ = _make_doc("PER-RV0014C", "20000017", sender, dest, [[(n1, 1)]])
    doc1.shipped_at = datetime(2024, 1, 1)
    doc2.shipped_at = datetime(2024, 1, 2)
    doc3.shipped_at = datetime(2024, 1, 3)
    db.session.commit()

    mock_orders.return_value = [_order("20000015", 714)]
    mock_items.return_value = _act_items({n1.barcode: 1})
    # Первый вызов time.monotonic() — точка отсчета (start); второй — перед
    # вторым документом, уже далеко за пределами RECEIVE_REVIEW_TIME_BUDGET.
    mock_monotonic.side_effect = [0, 1000]

    resp = client_logged_in.post(
        "/movement/receive-review/run",
        data={"doc_ids": [str(doc1.id), str(doc2.id), str(doc3.id)]},
    )
    html = resp.get_data(as_text=True)
    assert "PER-RV0014A" in html
    assert "Продолжить проверку" in html
    assert "еще не проверено: 2" in html
    mock_orders.assert_called_once()

    db.session.refresh(doc1)
    db.session.refresh(doc2)
    db.session.refresh(doc3)
    assert doc1.received_at is not None
    assert doc2.received_at is None
    assert doc3.received_at is None


@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_uses_short_timeout_and_retries_not_ozon_api_defaults(mock_orders, mock_items, db, client_logged_in):
    """Обычные ozon_api.REQUEST_TIMEOUT=60с и MAX_RETRIES=5 дают больше 5
    минут на один зависший вызов — именно это держало 504 даже на
    маленьких пачках. Интерактивная кнопка должна требовать от
    ozon_api короткий бюджет, а не ждать дефолтные значения."""
    sender, dest = _warehouses(15)
    n1 = _nomenclature(15)
    doc, _ = _make_doc("PER-RV0015", "20000018", sender, dest, [[(n1, 1)]])
    mock_orders.return_value = [_order("20000018", 715)]
    mock_items.return_value = _act_items({n1.barcode: 1})

    client_logged_in.post("/movement/receive-review/run", data={"doc_ids": [str(doc.id)]})

    _, kwargs = mock_orders.call_args
    assert kwargs["timeout"] == RECEIVE_REVIEW_CALL_TIMEOUT
    assert kwargs["max_retries"] == RECEIVE_REVIEW_CALL_MAX_RETRIES

    _, kwargs = mock_items.call_args
    assert kwargs["timeout"] == RECEIVE_REVIEW_CALL_TIMEOUT
    assert kwargs["max_retries"] == RECEIVE_REVIEW_CALL_MAX_RETRIES


@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_identified_surplus_is_accepted_and_added_to_box(mock_orders, mock_items, db, client_logged_in):
    from wms.models import MovementReceiptDiscrepancy, UnplacedStock

    sender, dest = _warehouses(21)
    n1 = _nomenclature(21, name="Шапка")
    n2 = _nomenclature(22, name="Шарф")
    db.session.add_all([
        UnplacedStock(warehouse_id=sender.id, nomenclature_id=n1.id, qty=10),
        UnplacedStock(warehouse_id=sender.id, nomenclature_id=n2.id, qty=10),
    ])
    db.session.commit()
    doc, boxes = _make_doc("PER-RV0021", "20000021", sender, dest, [[(n1, 10)], [(n1, 4)]])
    mock_orders.return_value = [_order("20000021", 721)]
    # Шапок на 2 больше, плюс 3 шарфа, которых в перемещении не было вовсе.
    mock_items.return_value = _act_items({n1.barcode: 16, n2.barcode: 3})

    resp = client_logged_in.post("/movement/receive-review/run", data={"doc_ids": [str(doc.id)]})
    html = resp.get_data(as_text=True)
    assert "Принято с излишком — излишек добавлен в короб (1)" in html
    assert "Требуется ручная проверка (0)" in html
    assert "table-warning" in html and "+2" in html and "+3" in html

    db.session.refresh(doc)
    assert doc.received_at is not None and doc.receipt_from_act
    assert doc.received_qty_snapshot == 19 and doc.sent_qty_snapshot == 14
    # Излишек шапок — в короб, где они уже есть (последний), шарфы — в последний короб.
    assert BoxItem.query.filter_by(box_id=boxes[1].id, nomenclature_id=n1.id).one().qty == 6
    assert BoxItem.query.filter_by(box_id=boxes[1].id, nomenclature_id=n2.id).one().qty == 3
    assert BoxItem.query.filter_by(box_id=boxes[0].id, nomenclature_id=n1.id).one().qty == 10
    # Излишек взялся с остатка отправителя.
    assert UnplacedStock.query.filter_by(warehouse_id=sender.id, nomenclature_id=n1.id).one().qty == 8
    assert UnplacedStock.query.filter_by(warehouse_id=sender.id, nomenclature_id=n2.id).one().qty == 7
    found = {(d.nomenclature_id, d.expected_qty, d.received_qty) for d in MovementReceiptDiscrepancy.query.filter_by(document_id=doc.id)}
    assert found == {(n1.id, 14, 16), (n2.id, 0, 3)}

    # «Вернуть в работу» снимает добавленное обратно.
    client_logged_in.post(f"/movement/{doc.id}/unreceive")
    db.session.expire_all()
    assert BoxItem.query.filter_by(box_id=boxes[1].id, nomenclature_id=n1.id).one().qty == 4
    assert BoxItem.query.filter_by(box_id=boxes[1].id, nomenclature_id=n2.id).first() is None
    assert UnplacedStock.query.filter_by(warehouse_id=sender.id, nomenclature_id=n1.id).one().qty == 10


@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_unidentified_surplus_goes_to_manual(mock_orders, mock_items, db, client_logged_in):
    sender, dest = _warehouses(23)
    n1 = _nomenclature(23)
    doc, _ = _make_doc("PER-RV0023", "20000023", sender, dest, [[(n1, 10)]])
    mock_orders.return_value = [_order("20000023", 723)]
    mock_items.return_value = _act_items({n1.barcode: 10, "4600000099999": 2})

    html = client_logged_in.post(
        "/movement/receive-review/run", data={"doc_ids": [str(doc.id)]}
    ).get_data(as_text=True)
    assert "Требуется ручная проверка (1)" in html
    db.session.refresh(doc)
    assert doc.received_at is None


@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_manual_check_shows_quantities_in_columns_and_highlights_diff(mock_orders, mock_items, db, client_logged_in):
    sender, dest = _warehouses(24)
    n1 = _nomenclature(24, name="Носки")
    n2 = _nomenclature(25, name="Варежки")
    doc, _ = _make_doc("PER-RV0024", "20000024", sender, dest, [[(n1, 10), (n2, 5)]])
    mock_orders.return_value = [_order("20000024", 724)]
    mock_items.return_value = _act_items({n1.barcode: 7, n2.barcode: 5})

    html = client_logged_in.post(
        "/movement/receive-review/run", data={"doc_ids": [str(doc.id)]}
    ).get_data(as_text=True)
    assert "<th class=\"text-end\">Отправлено</th>" in html and "<th class=\"text-end\">Принято</th>" in html
    assert "table-danger" in html and ">-3<" in html
    assert "Варежки" in html     # совпавшие позиции тоже видны, без подсветки


@patch("wms.blueprints.movement.threading.Thread")
@patch("wms.blueprints.movement.fetch_supply_act_items")
@patch("wms.blueprints.movement.fetch_supply_orders")
def test_server_mode_runs_in_background_and_shows_result(mock_orders, mock_items, mock_thread, app, db, client_logged_in):
    """На сервере проверка идет в фоне (иначе 504): кнопка сразу отдает
    страницу хода проверки, результат открывается по готовности."""
    from wms.models import BackgroundJob

    sender, dest = _warehouses(31)
    n1 = _nomenclature(31)
    doc, _ = _make_doc("PER-RV0031", "20000031", sender, dest, [[(n1, 10)]])
    mock_orders.return_value = [_order("20000031", 731)]
    mock_items.return_value = _act_items({n1.barcode: 8})

    app.config["RECEIVE_REVIEW_INLINE"] = False
    try:
        resp = client_logged_in.post("/movement/receive-review/run", data={"doc_ids": [str(doc.id)]})
    finally:
        app.config.pop("RECEIVE_REVIEW_INLINE")
    assert resp.status_code == 302 and "/receive-review/job/" in resp.headers["Location"]
    job = BackgroundJob.query.one()
    assert job.status == "running"
    assert "Идёт проверка" in client_logged_in.get(resp.headers["Location"]).get_data(as_text=True)

    # Выполняем то, что сделал бы поток.
    target = mock_thread.call_args.kwargs["target"]
    target(*mock_thread.call_args.kwargs["args"])

    db.session.expire_all()
    job = BackgroundJob.query.one()
    assert job.status == "done" and job.progress_done == 1
    status = client_logged_in.get(f"/movement/receive-review/job/{job.id}/status").get_json()
    assert status == {"status": "done", "done": 1, "total": 1}
    html = client_logged_in.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Требуется ручная проверка (1)" in html and "PER-RV0031" in html and ">-2<" in html
