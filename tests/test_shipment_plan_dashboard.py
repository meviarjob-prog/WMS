"""Дашборд плана отгрузок должен учитывать короба, которые уже уехали
перемещением (документ завершен), но еще не подтверждены кнопкой "Принято
на складе" — они висят "в пути" и до приемки не увеличивают fulfilled_qty
строки плана. Раньше это было видно только в сводной таблице по городу
целиком, а раздел "Что нужно отправить" (по конкретному товару) требовал
полное количество по плану, как будто в пути ничего нет."""

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    Nomenclature,
    ShipmentPlan,
    ShipmentPlanLine,
    Warehouse,
)


def _setup(planned_qty=30):
    sender = Warehouse(code="WH-D1", name="Склад-отправитель")
    city = Warehouse(code="WH-D2", name="ОЗОН: Город", marketplace="ozon", marketplace_city="Город")
    db.session.add_all([sender, city])
    db.session.commit()

    item = Nomenclature(sku="SKU-D1", barcode="7770000001", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    plan = ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    db.session.commit()
    line = ShipmentPlanLine(
        plan_id=plan.id,
        warehouse_id=city.id,
        nomenclature_id=item.id,
        barcode=item.barcode,
        article="ART-1",
        planned_qty=planned_qty,
        fulfilled_qty=0,
    )
    db.session.add(line)
    db.session.commit()
    return sender, city, item


def _ship_box(sender, city, item, qty, box_number, client):
    box = Box(box_number=box_number, warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
    db.session.commit()

    doc = MovementDocument(number=f"PER-{box_number}", from_warehouse_id=sender.id, to_warehouse_id=city.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=box.warehouse_id, from_cell_id=box.cell_id)
    )
    db.session.commit()

    client.post(f"/movement/{doc.id}/complete")
    return doc


def test_dashboard_top_summary_shows_in_transit(db, client_logged_in):
    sender, city, item = _setup(planned_qty=30)
    doc = _ship_box(sender, city, item, qty=10, box_number="BOX-000001", client=client_logged_in)

    doc = MovementDocument.query.get(doc.id)
    assert doc.status == "completed"
    assert doc.received_at is None

    resp = client_logged_in.get("/shipment-plan/")
    html = resp.get_data(as_text=True)
    idx = html.find("Город")
    assert "10" in html[idx : idx + 400]


def test_picking_list_nets_out_in_transit_quantity(db, client_logged_in):
    """Раньше здесь показывалось бы полное 30 — теперь 30 - 10 = 20,
    и рядом должна быть пометка, что 10 уже в пути."""
    sender, city, item = _setup(planned_qty=30)
    _ship_box(sender, city, item, qty=10, box_number="BOX-000002", client=client_logged_in)

    resp = client_logged_in.get("/shipment-plan/")
    html = resp.get_data(as_text=True)
    idx = html.find("ART-1")
    snippet = html[idx : idx + 1500]

    assert ">20.0<" in snippet
    assert "в пути" in snippet
    assert ">30.0<" not in snippet


def test_picking_list_drops_item_fully_covered_by_in_transit(db, client_logged_in):
    """Если все, что нужно, уже едет (в пути >= план), позиция не должна
    висеть в "Что нужно отправить" как будто требуется полное количество."""
    sender, city, item = _setup(planned_qty=10)
    _ship_box(sender, city, item, qty=10, box_number="BOX-000003", client=client_logged_in)

    resp = client_logged_in.get("/shipment-plan/")
    html = resp.get_data(as_text=True)
    assert "ART-1" not in html
