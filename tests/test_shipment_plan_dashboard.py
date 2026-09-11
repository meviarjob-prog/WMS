"""Дашборд плана отгрузок должен показывать, сколько по каждой позиции уже
"в пути" (отправлено перемещением, но еще не подтверждено кнопкой "Принято
на складе") — отдельным числом рядом с потребностью. Сама потребность
(remaining_qty) при этом не меняется: пока товар физически не проверен на
складе назначения, план по нему остается открытым, ровно как и
fulfilled_qty, который тоже засчитывается только по факту приемки."""

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


def test_picking_list_keeps_full_demand_and_shows_in_transit_separately(db, client_logged_in):
    """Пока короб не принят на складе назначения, потребность в "Что нужно
    отправить" остается полной (30, а не 30-10) — "в пути" показывается
    рядом отдельным числом, а не вычитается."""
    sender, city, item = _setup(planned_qty=30)
    _ship_box(sender, city, item, qty=10, box_number="BOX-000002", client=client_logged_in)

    resp = client_logged_in.get("/shipment-plan/")
    html = resp.get_data(as_text=True)
    idx = html.find("ART-1")
    snippet = html[idx : idx + 3000]

    assert ">30<" in snippet
    assert "(10)" in snippet
    assert ">20<" not in snippet


def test_top_summary_shows_in_transit_per_marketplace_and_total(db, client_logged_in):
    sender, city, item = _setup(planned_qty=30)
    _ship_box(sender, city, item, qty=10, box_number="BOX-SUM-1", client=client_logged_in)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    assert "Сводка" in html
    idx = html.find("В пути")
    snippet = html[idx : idx + 600]
    assert "ОЗОН" in snippet
    assert ">10<" in snippet
    assert "Итого" in snippet


def test_top_summary_shows_total_stock(db, client_logged_in):
    from wms.models import UnplacedStock

    sender, city, item = _setup(planned_qty=30)
    UnplacedStock.add(sender.id, item.id, 25)
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    idx = html.find("На складе:")
    assert "25" in html[idx : idx + 100]


def test_top_summary_shows_total_production_since_period_start(db, client_logged_in):
    from datetime import date, timedelta

    from wms.models import ProductionRecord, User

    sender, city, item = _setup(planned_qty=30)
    plan = ShipmentPlan.query.filter_by(marketplace="ozon").first()
    plan.period_start = date.today() - timedelta(days=3)
    db.session.commit()

    worker = User(username="prod-worker", role="production")
    worker.set_password("x")
    db.session.add(worker)
    db.session.commit()

    # В периоде плана — должно засчитаться.
    db.session.add(
        ProductionRecord(user_id=worker.id, nomenclature_id=item.id, work_date=date.today())
    )
    # До начала периода — не должно засчитаться.
    db.session.add(
        ProductionRecord(
            user_id=worker.id, nomenclature_id=item.id, work_date=date.today() - timedelta(days=10)
        )
    )
    db.session.commit()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    idx = html.find("На производстве")
    assert ">1<" in html[idx : idx + 200]


def test_marketplace_header_fulfilled_includes_in_transit(db, client_logged_in):
    sender, city, item = _setup(planned_qty=30)
    line = ShipmentPlanLine.query.first()
    line.fulfilled_qty = 5
    db.session.commit()
    _ship_box(sender, city, item, qty=10, box_number="BOX-SUM-2", client=client_logged_in)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    # Выполнено должно быть 5 (принято) + 10 (в пути) = 15, а не просто 5.
    idx = html.find("выполнено")
    snippet = html[idx : idx + 200]
    assert "15" in snippet


def test_picking_list_has_totals_row_summing_columns(db, client_logged_in):
    """Строка "Итого" сразу под заголовками — просто сумма по столбцам
    (На разбраковке/Готово к отгрузке/В пути и по каждому городу)."""
    from wms.models import UnplacedStock

    sender, city, item = _setup(planned_qty=30)
    UnplacedStock.add(sender.id, item.id, 8)
    db.session.commit()
    _ship_box(sender, city, item, qty=10, box_number="BOX-TOT-1", client=client_logged_in)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    idx = html.find("Итого (")
    assert idx != -1
    snippet = html[idx : idx + 800]
    assert "1 поз." in snippet
    assert ">8<" in snippet  # На разбраковке
    assert ">30<" in snippet  # Казань remaining_qty (30, полная потребность)
    assert ">10<" in snippet  # В пути


def test_picking_list_has_v_puti_column(db, client_logged_in):
    sender, city, item = _setup(planned_qty=30)
    _ship_box(sender, city, item, qty=10, box_number="BOX-TOT-2", client=client_logged_in)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    idx = html.find("В пути")
    assert idx != -1
    idx2 = html.find("ART-1")
    row_snippet = html[idx2 : idx2 + 1200]
    assert "10" in row_snippet


def test_totals_row_not_hidden_by_search_filter(db, client_logged_in):
    sender, city, item = _setup(planned_qty=30)
    _ship_box(sender, city, item, qty=10, box_number="BOX-TOT-3", client=client_logged_in)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    assert "picking-totals-row" in html


def test_picking_list_keeps_item_even_when_fully_in_transit(db, client_logged_in):
    """Даже если все нужное количество уже едет (в пути >= план), позиция
    не пропадает из "Что нужно отправить" — потребность закрывается только
    приемкой ("Принято на складе"), не отправкой."""
    sender, city, item = _setup(planned_qty=10)
    _ship_box(sender, city, item, qty=10, box_number="BOX-000003", client=client_logged_in)

    resp = client_logged_in.get("/shipment-plan/")
    html = resp.get_data(as_text=True)
    assert "ART-1" in html
    idx = html.find("ART-1")
    snippet = html[idx : idx + 3000]
    assert ">10<" in snippet
    assert "(10)" in snippet
