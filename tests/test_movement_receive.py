"""«Принято на складе» — единственная кнопка приемки перемещения (см. чат:
раньше рядом была отдельная «Принято с расхождением», ее убрали — эта форма
покрывает оба случая: обычную приемку и приемку с недостачей/излишком).
См. movement.receive: указанное на этой форме фактическое количество
зачитывается в план отгрузок вместо количества из коробов, а расхождение
(если оно есть) сохраняется отдельной строкой (MovementReceiptDiscrepancy).
Отметка "заявка на маркетплейс создана" обязательна перед приемкой для
ВСЕХ перемещений (см. чат) — без нее кнопка/форма приемки недоступна."""

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    MovementReceiptDiscrepancy,
    Nomenclature,
    ShipmentPlan,
    ShipmentPlanLine,
    Warehouse,
)


def _make_completed_document(qty=10, marketplace_request_created=True):
    sender = Warehouse(code="WH-D1A", name="Склад-отправитель")
    dest = Warehouse(code="WH-D1B", name="ОЗОН: Тверь", marketplace="ozon", marketplace_city="Тверь")
    db.session.add_all([sender, dest])
    db.session.commit()

    item = Nomenclature(sku="SKU-D1", barcode="6660000001", name="Товар для расхождения", unit="шт")
    db.session.add(item)
    db.session.commit()

    plan = ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    db.session.commit()
    plan_line = ShipmentPlanLine(
        plan_id=plan.id, warehouse_id=dest.id, nomenclature_id=item.id,
        barcode=item.barcode, planned_qty=100, fulfilled_qty=0,
    )
    db.session.add(plan_line)

    box = Box(box_number="BOX-D00001", warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))

    doc = MovementDocument(
        number="PER-D0001", from_warehouse_id=sender.id, to_warehouse_id=dest.id,
        status="completed",
    )
    if marketplace_request_created:
        from datetime import datetime

        doc.marketplace_request_created_at = datetime.utcnow()
        doc.marketplace_request_number = "REQ-D0001"
        doc.shipped_at = datetime.utcnow()
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id)
    )
    db.session.commit()
    return doc, item, plan_line


def test_receive_form_shows_expected_quantities(db, client_logged_in):
    doc, item, _plan_line = _make_completed_document(qty=10)

    resp = client_logged_in.get(f"/movement/{doc.id}/receive")

    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert item.name in html
    assert 'name="qty_' in html


def test_shortage_credits_actual_received_qty_and_records_discrepancy(db, client_logged_in):
    doc, item, plan_line = _make_completed_document(qty=10)

    resp = client_logged_in.post(
        f"/movement/{doc.id}/receive",
        data={f"qty_{item.id}": "7"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    doc = MovementDocument.query.get(doc.id)
    assert doc.received_at is not None

    plan_line = ShipmentPlanLine.query.get(plan_line.id)
    assert plan_line.fulfilled_qty == 7

    discrepancy = MovementReceiptDiscrepancy.query.filter_by(document_id=doc.id).first()
    assert discrepancy is not None
    assert discrepancy.expected_qty == 10
    assert discrepancy.received_qty == 7
    assert discrepancy.diff() == -3
    assert discrepancy.shortage_qty() == 3
    assert doc.total_received_qty() == 7
    assert doc.total_shortage_qty() == 3

    movement_list = client_logged_in.get("/movement/").get_data(as_text=True)
    assert 'title="Фактически принято на маркетплейсе">(7)</span>' in movement_list
    assert "Недовоз 3" in movement_list

    shortage_report = client_logged_in.get("/reports/movement-shortages")
    shortage_html = shortage_report.get_data(as_text=True)
    assert shortage_report.status_code == 200
    assert doc.number in shortage_html
    assert f'href="/movement/{doc.id}"' in shortage_html
    assert "Товар для расхождения" not in shortage_html

    detail_html = client_logged_in.get(f"/movement/{doc.id}").get_data(as_text=True)
    assert "Недовоз — товар нужно найти" in detail_html
    assert item.barcode in detail_html
    assert item.name in detail_html
    assert "Нужно найти" in detail_html


def test_excess_credits_actual_received_qty_and_records_discrepancy(db, client_logged_in):
    doc, item, plan_line = _make_completed_document(qty=10)

    client_logged_in.post(
        f"/movement/{doc.id}/receive",
        data={f"qty_{item.id}": "12"},
    )

    plan_line = ShipmentPlanLine.query.get(plan_line.id)
    assert plan_line.fulfilled_qty == 12

    discrepancy = MovementReceiptDiscrepancy.query.filter_by(document_id=doc.id).first()
    assert discrepancy.diff() == 2
    assert discrepancy.shortage_qty() == 0
    shortage_html = client_logged_in.get(
        "/reports/movement-shortages"
    ).get_data(as_text=True)
    assert f'href="/movement/{doc.id}" class="fw-bold">{doc.number}</a>' not in shortage_html


def test_request_number_alone_does_not_allow_receiving(db, client_logged_in):
    doc, item, _plan_line = _make_completed_document(
        qty=10, marketplace_request_created=False
    )
    doc.marketplace_request_number = "REQ-ONLY-123"
    db.session.commit()

    response = client_logged_in.post(
        f"/movement/{doc.id}/receive",
        data={f"qty_{item.id}": "10"},
    )

    assert response.status_code == 302
    assert MovementDocument.query.get(doc.id).received_at is None


def test_matching_quantity_creates_no_discrepancy_row(db, client_logged_in):
    doc, item, plan_line = _make_completed_document(qty=10)

    client_logged_in.post(
        f"/movement/{doc.id}/receive",
        data={f"qty_{item.id}": "10"},
    )

    plan_line = ShipmentPlanLine.query.get(plan_line.id)
    assert plan_line.fulfilled_qty == 10
    assert MovementReceiptDiscrepancy.query.filter_by(document_id=doc.id).count() == 0


def test_cannot_receive_before_document_completed(db, client_logged_in):
    sender = Warehouse(code="WH-D2A", name="Склад-отправитель 2")
    dest = Warehouse(code="WH-D2B", name="ОЗОН: Уфа")
    db.session.add_all([sender, dest])
    db.session.commit()
    doc = MovementDocument(number="PER-D0002", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()

    resp = client_logged_in.get(f"/movement/{doc.id}/receive", follow_redirects=True)

    assert "Сначала завершите перемещение" in resp.get_data(as_text=True)


def test_cannot_receive_twice(db, client_logged_in):
    doc, item, _plan_line = _make_completed_document(qty=10)
    client_logged_in.post(f"/movement/{doc.id}/receive", data={f"qty_{item.id}": "10"})

    resp = client_logged_in.get(f"/movement/{doc.id}/receive", follow_redirects=True)

    assert "уже отмечено как принятое" in resp.get_data(as_text=True)


def test_cannot_receive_without_marketplace_request_created(db, client_logged_in):
    """Отметка "заявка на МП создана" обязательна для ВСЕХ перемещений перед
    приемкой — без нее форма/кнопка "Принято на складе" недоступна вовсе,
    даже POST напрямую по ссылке отклоняется."""
    doc, item, _plan_line = _make_completed_document(qty=10, marketplace_request_created=False)

    resp = client_logged_in.get(f"/movement/{doc.id}/receive", follow_redirects=True)
    assert "Сначала внесите номер заявки на маркетплейс" in resp.get_data(as_text=True)
    assert MovementDocument.query.get(doc.id).received_at is None

    resp = client_logged_in.post(
        f"/movement/{doc.id}/receive", data={f"qty_{item.id}": "10"}, follow_redirects=True
    )
    assert "Сначала внесите номер заявки на маркетплейс" in resp.get_data(as_text=True)
    assert MovementDocument.query.get(doc.id).received_at is None


def test_marking_marketplace_request_unblocks_receive(db, client_logged_in):
    doc, item, _plan_line = _make_completed_document(qty=10, marketplace_request_created=False)
    doc.marketplace_request_number = "REQ-UNBLOCK"
    db.session.commit()

    client_logged_in.post(f"/movement/{doc.id}/mark-marketplace-request")
    assert MovementDocument.query.get(doc.id).marketplace_request_created_at is not None
    client_logged_in.post(f"/movement/{doc.id}/mark-shipped")
    assert MovementDocument.query.get(doc.id).shipped_at is not None

    resp = client_logged_in.post(
        f"/movement/{doc.id}/receive", data={f"qty_{item.id}": "10"}, follow_redirects=True
    )
    assert resp.status_code == 200
    assert MovementDocument.query.get(doc.id).received_at is not None


def test_admin_returns_received_movement_to_work_and_receives_again(db, client_logged_in):
    doc, item, plan_line = _make_completed_document(qty=10)
    client_logged_in.post(f"/movement/{doc.id}/receive", data={f"qty_{item.id}": "7"})
    assert "Вернуть в работу" in client_logged_in.get(f"/movement/{doc.id}").get_data(as_text=True)

    client_logged_in.post(f"/movement/{doc.id}/unreceive")
    db.session.expire_all()
    doc = MovementDocument.query.get(doc.id)
    assert doc.received_at is None and doc.received_qty_snapshot is None
    assert MovementReceiptDiscrepancy.query.filter_by(document_id=doc.id).count() == 0
    assert BoxItem.query.filter_by(nomenclature_id=item.id).one().qty == 10  # недовоз возвращен в короб
    assert ShipmentPlanLine.query.get(plan_line.id).fulfilled_qty == 0

    client_logged_in.post(f"/movement/{doc.id}/receive", data={f"qty_{item.id}": "10"})
    db.session.expire_all()
    assert MovementDocument.query.get(doc.id).received_at is not None
    assert ShipmentPlanLine.query.get(plan_line.id).fulfilled_qty == 10
    assert MovementReceiptDiscrepancy.query.filter_by(document_id=doc.id).count() == 0


def test_shortage_reduces_box_and_shows_sent_vs_received(db, client_logged_in):
    """Недовоз при приемке списывается из короба этого перемещения (см.
    чат): отправлено 10, принято 7 → «10 (7)», в коробе остается 7; короб
    не трогаем только у неразмещенного остатка — туда недовоз не попадает,
    он просто недостача, которую можно найти и разместить отдельно."""
    from wms.models import UnplacedStock
    doc, item, _ = _make_completed_document(qty=10)
    client_logged_in.post(f"/movement/{doc.id}/receive", data={f"qty_{item.id}": "7"})
    db.session.expire_all()
    doc = MovementDocument.query.get(doc.id)
    assert BoxItem.query.filter_by(nomenclature_id=item.id).one().qty == 7
    assert (doc.total_sent_qty(), doc.total_received_qty()) == (10, 7)
    doc.received_qty_snapshot = None
    db.session.commit()
    assert MovementDocument.query.get(doc.id).total_received_qty() == 7
    page = client_logged_in.get("/movement/").get_data(as_text=True)
    assert 'title="Фактически принято на маркетплейсе">(7)</span>' in page
    assert UnplacedStock.query.count() == 0


def test_sent_snapshot_refreshes_if_box_edited_between_complete_and_receive(db, client_logged_in):
    """Регрессия: complete() ставит sent_qty_snapshot по короба на тот
    момент; если короб поправили ДО приемки (а не после), снимок должен
    обновиться на актуальное содержимое при приемке — иначе он навсегда
    остается старым (с complete()) и расходится с расхождением, которое
    считается по уже новому содержимому короба (см. чат: бейдж "Излишек"
    в списке при видимом в скобках недовозе — total_sent_qty() показывал
    устаревшие 380, а не то, что было в коробе на момент приемки)."""
    doc, item, _ = _make_completed_document(qty=10)
    # _make_completed_document создает документ уже в статусе "completed"
    # напрямую, без снимка — сбросим в draft и завершим настоящим роутом,
    # чтобы complete() реально поставил sent_qty_snapshot, как в жизни.
    doc.status = "draft"
    doc.sent_qty_snapshot = None
    db.session.commit()
    client_logged_in.post(f"/movement/{doc.id}/complete")
    db.session.expire_all()
    doc = MovementDocument.query.get(doc.id)
    assert doc.sent_qty_snapshot == 10

    # Короб поправили ДО приемки (например, заметили ошибку упаковки) —
    # отправили по факту меньше, чем было изначально собрано.
    box_item = doc.lines.first().box.items.first()
    box_item.qty = 6
    db.session.commit()

    # Приняли ровно столько, сколько реально было в коробе на этот момент —
    # расхождения относительно НОВОГО содержимого нет вообще.
    client_logged_in.post(f"/movement/{doc.id}/receive", data={f"qty_{item.id}": "6"})

    db.session.expire_all()
    doc = MovementDocument.query.get(doc.id)
    assert doc.sent_qty_snapshot == 6  # не устаревшие 10
    assert (doc.total_sent_qty(), doc.total_received_qty()) == (6, 6)
    from wms.models import MovementReceiptDiscrepancy

    assert MovementReceiptDiscrepancy.query.filter_by(document_id=doc.id).count() == 0


def test_editing_box_qty_after_receipt_does_not_shift_sent_or_received(db, client_logged_in):
    """Регрессия (см. чат): "в перемещениях в скобках указываем фактически
    принятое кол-во на СЦ... если поменять кол-во в перемещении, то он
    минусует еще больше — он не должен быть привязан к основному
    перемещению". После приемки "отправлено"/"принято" — зафиксированный
    факт на момент приемки, а не живое содержимое короба: правка короба
    по ЛЮБОЙ другой причине (например, через админскую правку количества
    в завершенной приемке) не должна задним числом менять эти цифры."""
    doc, item, _ = _make_completed_document(qty=10)
    client_logged_in.post(f"/movement/{doc.id}/receive", data={f"qty_{item.id}": "7"})
    db.session.expire_all()
    doc = MovementDocument.query.get(doc.id)
    assert (doc.total_sent_qty(), doc.total_received_qty()) == (10, 7)

    # Короб поправили уже ПОСЛЕ приемки по совершенно другой причине —
    # например, администратор обнаружил и исправил ошибку задним числом.
    box_item = doc.lines.first().box.items.first()
    box_item.qty = 2
    db.session.commit()

    doc = MovementDocument.query.get(doc.id)
    assert doc.total_item_qty() == 2  # живое содержимое короба и правда изменилось
    assert (doc.total_sent_qty(), doc.total_received_qty()) == (10, 7)  # но витрина — нет

    page = client_logged_in.get("/movement/").get_data(as_text=True)
    assert 'title="Фактически принято на маркетплейсе">(7)</span>' in page


def test_receive_accepts_unexpected_nomenclature_as_pure_excess(db, client_logged_in):
    """Приемка на СЦ позволяет указать товар, которого вообще не было в
    отправке (новая номенклатура через extra_nomenclature_id) — он
    учитывается как чистый излишек: expected_qty=0, списывается с
    неразмещенного остатка склада-отправителя (см. чат)."""
    from wms.models import MovementReceiptDiscrepancy, Nomenclature, UnplacedStock

    doc, item, plan_line = _make_completed_document(qty=10)
    extra_item = Nomenclature(sku="SKU-EXTRA-1", barcode="6660000777", name="Незаявленный товар", unit="шт")
    db.session.add(extra_item)
    db.session.commit()
    UnplacedStock.add(doc.from_warehouse_id, extra_item.id, 20)

    resp = client_logged_in.post(
        f"/movement/{doc.id}/receive",
        data={
            f"qty_{item.id}": "10",
            "extra_nomenclature_id": str(extra_item.id),
            f"qty_{extra_item.id}": "4",
        },
        follow_redirects=True,
    )

    assert resp.status_code == 200
    discrepancy = MovementReceiptDiscrepancy.query.filter_by(
        document_id=doc.id, nomenclature_id=extra_item.id
    ).first()
    assert discrepancy is not None
    assert discrepancy.expected_qty == 0
    assert discrepancy.received_qty == 4
    assert discrepancy.excess_qty() == 4
    assert UnplacedStock.available(doc.from_warehouse_id, extra_item.id) == 16


def test_receive_ignores_extra_nomenclature_without_qty_or_unknown_id(db, client_logged_in):
    from wms.models import MovementReceiptDiscrepancy, Nomenclature

    doc, item, plan_line = _make_completed_document(qty=10)
    extra_item = Nomenclature(sku="SKU-EXTRA-2", barcode="6660000778", name="Товар без кол-ва", unit="шт")
    db.session.add(extra_item)
    db.session.commit()

    resp = client_logged_in.post(
        f"/movement/{doc.id}/receive",
        data={
            f"qty_{item.id}": "10",
            "extra_nomenclature_id": [str(extra_item.id), "999999"],
            # qty_<extra_item.id> не передано вовсе — строка должна быть пропущена.
        },
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert MovementReceiptDiscrepancy.query.filter_by(document_id=doc.id).count() == 0


def test_excess_debited_from_sender_unplaced_stock_box_unchanged(db, client_logged_in):
    """Излишек при приемке не трогает короб (в нем как было упаковано, так
    и остается) — физически со склада-отправителя увезли больше, чем
    записано в коробе, поэтому списываем излишек с его неразмещенного
    остатка (см. чат: "он наоборот должен списываться, если отвезли
    лишнее"), а не добавляем остаток складу назначения."""
    from wms.models import UnplacedStock
    doc, item, _ = _make_completed_document(qty=10)
    UnplacedStock.add(doc.from_warehouse_id, item.id, 5)

    client_logged_in.post(f"/movement/{doc.id}/receive", data={f"qty_{item.id}": "12"})

    db.session.expire_all()
    doc = MovementDocument.query.get(doc.id)
    assert (doc.total_sent_qty(), doc.total_received_qty()) == (10, 12)
    assert BoxItem.query.filter_by(nomenclature_id=item.id).one().qty == 10
    assert UnplacedStock.available(doc.from_warehouse_id, item.id) == 3  # 5 - 2 излишка
    assert UnplacedStock.available(doc.to_warehouse_id, item.id) == 0


def test_old_receipt_that_reduced_boxes_still_shows_right_numbers(db, client_logged_in):
    """Старая приемка (до изменения) списала недовоз из коробов: 10 → 7.
    Показываем «10 (7)», а не «7 (4)»."""
    from datetime import datetime
    doc, item, _ = _make_completed_document(qty=10)
    BoxItem.query.filter_by(nomenclature_id=item.id).one().qty = 7
    doc.received_at = datetime.utcnow()
    db.session.add(MovementReceiptDiscrepancy(document_id=doc.id, nomenclature_id=item.id, expected_qty=10, received_qty=7))
    db.session.commit()
    doc = MovementDocument.query.get(doc.id)
    assert doc.receipt_changed_boxes is None
    assert (doc.total_sent_qty(), doc.total_received_qty()) == (10, 7)
