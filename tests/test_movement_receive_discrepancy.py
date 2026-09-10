"""«Принято с расхождением» — альтернатива обычной «Принято на складе»,
когда на месте приняли не столько, сколько отправили (недостача/излишек).
См. movement.receive_with_discrepancy: указанное на этой форме фактическое
количество зачитывается в план отгрузок вместо количества из коробов, а
расхождение сохраняется отдельной строкой (MovementReceiptDiscrepancy)."""

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


def _make_completed_document(qty=10):
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
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id)
    )
    db.session.commit()
    return doc, item, plan_line


def test_discrepancy_form_shows_expected_quantities(db, client_logged_in):
    doc, item, _plan_line = _make_completed_document(qty=10)

    resp = client_logged_in.get(f"/movement/{doc.id}/receive-with-discrepancy")

    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert item.name in html
    assert 'name="qty_' in html


def test_shortage_credits_actual_received_qty_and_records_discrepancy(db, client_logged_in):
    doc, item, plan_line = _make_completed_document(qty=10)

    resp = client_logged_in.post(
        f"/movement/{doc.id}/receive-with-discrepancy",
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


def test_excess_credits_actual_received_qty_and_records_discrepancy(db, client_logged_in):
    doc, item, plan_line = _make_completed_document(qty=10)

    client_logged_in.post(
        f"/movement/{doc.id}/receive-with-discrepancy",
        data={f"qty_{item.id}": "12"},
    )

    plan_line = ShipmentPlanLine.query.get(plan_line.id)
    assert plan_line.fulfilled_qty == 12

    discrepancy = MovementReceiptDiscrepancy.query.filter_by(document_id=doc.id).first()
    assert discrepancy.diff() == 2


def test_matching_quantity_creates_no_discrepancy_row(db, client_logged_in):
    doc, item, plan_line = _make_completed_document(qty=10)

    client_logged_in.post(
        f"/movement/{doc.id}/receive-with-discrepancy",
        data={f"qty_{item.id}": "10"},
    )

    plan_line = ShipmentPlanLine.query.get(plan_line.id)
    assert plan_line.fulfilled_qty == 10
    assert MovementReceiptDiscrepancy.query.filter_by(document_id=doc.id).count() == 0


def test_cannot_receive_with_discrepancy_before_document_completed(db, client_logged_in):
    sender = Warehouse(code="WH-D2A", name="Склад-отправитель 2")
    dest = Warehouse(code="WH-D2B", name="ОЗОН: Уфа")
    db.session.add_all([sender, dest])
    db.session.commit()
    doc = MovementDocument(number="PER-D0002", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()

    resp = client_logged_in.get(f"/movement/{doc.id}/receive-with-discrepancy", follow_redirects=True)

    assert "Сначала завершите перемещение" in resp.get_data(as_text=True)


def test_cannot_receive_with_discrepancy_twice(db, client_logged_in):
    doc, item, _plan_line = _make_completed_document(qty=10)
    client_logged_in.post(f"/movement/{doc.id}/receive-with-discrepancy", data={f"qty_{item.id}": "10"})

    resp = client_logged_in.get(f"/movement/{doc.id}/receive-with-discrepancy", follow_redirects=True)

    assert "уже отмечено как принятое" in resp.get_data(as_text=True)
