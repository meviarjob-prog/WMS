"""«Куда везти короб»: рекомендация склада-города по остатку невыполненной
потребности плана отгрузок, с учетом уже едущих туда (но не принятых)
коробов — см. обсуждение "нужно 30, отсканировали короб с 10"."""

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    MovementDocument,
    Nomenclature,
    ShipmentPlan,
    ShipmentPlanLine,
    Warehouse,
)


def _setup_plan(planned_qty=30, fulfilled_qty=0):
    sender = Warehouse(code="WH-SND", name="Склад-отправитель")
    city = Warehouse(code="WH-CTY", name="ОЗОН: Город", marketplace="ozon", marketplace_city="Город")
    db.session.add_all([sender, city])
    db.session.commit()

    item = Nomenclature(sku="SKU-R1", barcode="5550000001", name="Товар", unit="шт")
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
        planned_qty=planned_qty,
        fulfilled_qty=fulfilled_qty,
    )
    db.session.add(line)
    db.session.commit()
    return sender, city, item


def _make_box(sender, item, qty, box_number):
    box = Box(box_number=box_number, warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
    db.session.commit()
    return box


def test_routing_recommends_city_with_demand(db, client_logged_in):
    sender, city, item = _setup_plan(planned_qty=30)
    box = _make_box(sender, item, qty=10, box_number="BOX-000001")

    resp = client_logged_in.get(f"/movement/route-box?box_number={box.box_number}")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "ОЗОН: Город" in html
    assert "30 шт." in html  # полная потребность, пока ничего еще не отправлено


def test_routing_subtracts_already_committed_boxes(db, client_logged_in):
    """Пример из обсуждения: нужно 30, короб с 10 уже добавлен в
    перемещение (черновик) — следующий скан должен увидеть остаток 20."""
    sender, city, item = _setup_plan(planned_qty=30)
    box1 = _make_box(sender, item, qty=10, box_number="BOX-000001")
    box2 = _make_box(sender, item, qty=5, box_number="BOX-000002")

    client_logged_in.post(
        "/movement/route-box/add", data={"box_id": box1.id, "to_warehouse_id": city.id}
    )

    resp = client_logged_in.get(f"/movement/route-box?box_number={box2.box_number}")
    html = resp.get_data(as_text=True)

    assert "20 шт." in html
    assert "30 шт." not in html


def test_routing_skips_box_with_no_demand_anywhere(db, client_logged_in):
    sender = Warehouse(code="WH-SND2", name="Склад-отправитель 2")
    db.session.add(sender)
    db.session.commit()
    item = Nomenclature(sku="SKU-R2", barcode="5550000002", name="Товар без потребности", unit="шт")
    db.session.add(item)
    db.session.commit()
    box = _make_box(sender, item, qty=1, box_number="BOX-000003")

    resp = client_logged_in.get(f"/movement/route-box?box_number={box.box_number}")

    assert "Пропускаем" in resp.get_data(as_text=True)


def test_routing_add_creates_draft_movement_and_reuses_it(db, client_logged_in):
    sender, city, item = _setup_plan(planned_qty=30)
    box1 = _make_box(sender, item, qty=10, box_number="BOX-000001")
    box2 = _make_box(sender, item, qty=5, box_number="BOX-000002")

    client_logged_in.post(
        "/movement/route-box/add", data={"box_id": box1.id, "to_warehouse_id": city.id}
    )
    client_logged_in.post(
        "/movement/route-box/add", data={"box_id": box2.id, "to_warehouse_id": city.id}
    )

    docs = MovementDocument.query.filter_by(
        from_warehouse_id=sender.id, to_warehouse_id=city.id
    ).all()
    assert len(docs) == 1  # второй вызов не создал новый документ
    assert docs[0].lines.count() == 2
