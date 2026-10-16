"""«Выполнение плана» на дашборде плана отгрузок (см. чат): колонка «Дата
плана» — дата, к которой нужно отгрузить план по конкретному городу,
проставляется вручную через календарь и хранится в
ShipmentPlanCityDeadline. Факт под датой — нарастающим итогом с начала
периода плана по эту дату ВКЛЮЧИТЕЛЬНО (см. чат: "отгружено к этой дате
включительно"), в штуках и в % от плана города. Смена даты сохраняется
через AJAX и возвращает HTML-фрагмент пересчитанных и пересортированных
(по дате, по возрастанию) строк, без редиректа — см. чат: "без
перезагрузки страницы". Плюс разбивка вклада каждого склада-отправителя
(в штуках и процентах) в то, что уже уехало на этот город."""

from datetime import date, datetime, time, timedelta

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    Nomenclature,
    ShipmentPlan,
    ShipmentPlanCityDeadline,
    ShipmentPlanLine,
    Warehouse,
)


def _setup(planned_qty=30):
    sender = Warehouse(code="WH-SPD1", name="Основной склад")
    city = Warehouse(code="WH-SPD2", name="ОЗОН: Город", marketplace="ozon", marketplace_city="Город")
    db.session.add_all([sender, city])
    db.session.commit()

    item = Nomenclature(sku="SKU-SPD1", barcode="7770005001", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    plan = ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    db.session.commit()
    line = ShipmentPlanLine(
        plan_id=plan.id, warehouse_id=city.id, nomenclature_id=item.id,
        barcode=item.barcode, article="ART-SPD1", planned_qty=planned_qty, fulfilled_qty=0,
    )
    db.session.add(line)
    db.session.commit()
    return plan, sender, city, item


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
    doc.marketplace_request_number = f"REQ-{box_number}"
    db.session.commit()
    client.post(f"/movement/{doc.id}/mark-marketplace-request")
    client.post(f"/movement/{doc.id}/mark-shipped")
    return doc


def _set_deadline(client, plan, warehouse, value):
    return client.post(f"/shipment-plan/{plan.id}/cities/{warehouse.id}/deadline", data={"ship_by_date": value})


def test_set_deadline_date_shows_on_dashboard(db, client_logged_in):
    plan, sender, city, item = _setup()

    target = (date.today() + timedelta(days=5)).isoformat()
    resp = _set_deadline(client_logged_in, plan, city, target)
    assert resp.status_code == 302

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    assert f'value="{target}"' in html

    deadline = ShipmentPlanCityDeadline.query.filter_by(plan_id=plan.id, warehouse_id=city.id).first()
    assert deadline is not None
    assert deadline.ship_by_date.isoformat() == target


def test_update_deadline_date_overwrites_previous(db, client_logged_in):
    plan, sender, city, item = _setup()
    first = (date.today() + timedelta(days=3)).isoformat()
    second = (date.today() + timedelta(days=10)).isoformat()

    _set_deadline(client_logged_in, plan, city, first)
    _set_deadline(client_logged_in, plan, city, second)

    assert ShipmentPlanCityDeadline.query.filter_by(plan_id=plan.id, warehouse_id=city.id).count() == 1
    deadline = ShipmentPlanCityDeadline.query.filter_by(plan_id=plan.id, warehouse_id=city.id).first()
    assert deadline.ship_by_date.isoformat() == second


def test_clearing_deadline_date_removes_it(db, client_logged_in):
    plan, sender, city, item = _setup()
    _set_deadline(client_logged_in, plan, city, (date.today() + timedelta(days=1)).isoformat())

    _set_deadline(client_logged_in, plan, city, "")

    assert ShipmentPlanCityDeadline.query.filter_by(plan_id=plan.id, warehouse_id=city.id).first() is None


def test_sender_warehouse_breakdown_shows_qty_and_percent(db, client_logged_in):
    plan, sender1, city, item = _setup(planned_qty=100)
    sender2 = Warehouse(code="WH-SPD3", name="Склад №2 (Шоссейная 167)")
    db.session.add(sender2)
    db.session.commit()

    _ship_box(sender1, city, item, qty=30, box_number="BOX-SPD-S1", client=client_logged_in)
    _ship_box(sender2, city, item, qty=10, box_number="BOX-SPD-S2", client=client_logged_in)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    city_idx = html.find("<td>Город</td>")
    snippet = html[city_idx : city_idx + 1500]
    assert "30" in snippet and "75%" in snippet  # 30 из 40 суммарно уехавших
    assert "10" in snippet and "25%" in snippet  # 10 из 40


def test_deadline_shows_cumulative_qty_through_that_date_inclusive(db, client_logged_in):
    """Факт под датой плана — нарастающим итогом с начала периода по эту
    дату ВКЛЮЧИТЕЛЬНО (см. чат), а не только за один день: более раннюю
    отгрузку внутри периода учитывает, более позднюю (после даты плана) —
    нет."""
    plan, sender, city, item = _setup(planned_qty=100)

    earlier_doc = _ship_box(sender, city, item, qty=12, box_number="BOX-SPD-EARLY", client=client_logged_in)
    on_date_doc = _ship_box(sender, city, item, qty=20, box_number="BOX-SPD-ONDATE", client=client_logged_in)
    later_doc = _ship_box(sender, city, item, qty=99, box_number="BOX-SPD-LATER", client=client_logged_in)

    period_start = date.today() - timedelta(days=5)
    deadline_day = date.today() - timedelta(days=2)

    plan.period_start = period_start
    db.session.commit()

    db.session.refresh(earlier_doc)
    db.session.refresh(on_date_doc)
    db.session.refresh(later_doc)
    earlier_doc.shipped_at = datetime.combine(deadline_day - timedelta(days=1), time(9, 0))
    on_date_doc.shipped_at = datetime.combine(deadline_day, time(12, 0))
    later_doc.shipped_at = datetime.combine(deadline_day + timedelta(days=1), time(9, 0))
    db.session.commit()

    _set_deadline(client_logged_in, plan, city, deadline_day.isoformat())

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    city_idx = html.find("<td>Город</td>")
    snippet = html[city_idx : city_idx + 900]
    assert "факт к дате" in snippet
    assert "<b>32</b>" in snippet  # 12 (раньше) + 20 (в эту дату) = 32, включительно
    assert "<b>131</b>" not in snippet  # не включает отгрузку ПОСЛЕ даты плана
    assert "(32%)" in snippet  # 32 из плана 100


def test_cities_sorted_by_deadline_date_ascending(db, client_logged_in):
    plan, sender, city_a, item = _setup(planned_qty=10)
    city_b = Warehouse(code="WH-SPD4", name="ОЗОН: Другой", marketplace="ozon", marketplace_city="Другой")
    city_c = Warehouse(code="WH-SPD5", name="ОЗОН: Третий", marketplace="ozon", marketplace_city="Третий")
    db.session.add_all([city_b, city_c])
    db.session.commit()
    db.session.add_all([
        ShipmentPlanLine(
            plan_id=plan.id, warehouse_id=city_b.id, nomenclature_id=item.id,
            barcode=item.barcode, article="ART-SPD1", planned_qty=10, fulfilled_qty=0,
        ),
        ShipmentPlanLine(
            plan_id=plan.id, warehouse_id=city_c.id, nomenclature_id=item.id,
            barcode=item.barcode, article="ART-SPD1", planned_qty=10, fulfilled_qty=0,
        ),
    ])
    db.session.commit()

    # Город (city_a) — дата позже, Другой (city_b) — дата раньше,
    # Третий (city_c) — вообще без даты (должен уйти в конец).
    _set_deadline(client_logged_in, plan, city_a, (date.today() + timedelta(days=10)).isoformat())
    _set_deadline(client_logged_in, plan, city_b, (date.today() + timedelta(days=1)).isoformat())

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    pos_b = html.find("<td>Другой</td>")
    pos_a = html.find("<td>Город</td>")
    pos_c = html.find("<td>Третий</td>")
    assert pos_b != -1 and pos_a != -1 and pos_c != -1
    assert pos_b < pos_a < pos_c


def test_ajax_deadline_update_returns_fragment_without_redirect(db, client_logged_in):
    plan, sender, city, item = _setup(planned_qty=50)
    target = (date.today() + timedelta(days=3)).isoformat()

    resp = client_logged_in.post(
        f"/shipment-plan/{plan.id}/cities/{city.id}/deadline",
        data={"ship_by_date": target},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert f'value="{target}"' in html
    assert "<td>Город</td>" in html


def test_sender_breakdown_empty_when_nothing_shipped(db, client_logged_in):
    plan, sender, city, item = _setup()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    city_idx = html.find("<td>Город</td>")
    snippet = html[city_idx : city_idx + 1500]
    assert snippet.count(">—<") >= 2  # оба склада-отправителя без вклада


def test_percent_column_is_collapsible_group_with_sender_columns(db, client_logged_in):
    """«% вып.» — группа: колонки по складам-отправителям помечены
    sender-col и по умолчанию скрыты классом senders-collapsed на таблице;
    кнопка в шапке разворачивает их (см. чат)."""
    plan, sender, city, item = _setup()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    assert "city-table senders-collapsed" in html
    assert "senders-toggle" in html
    assert 'class="sender-col"' in html


def test_ajax_fragment_keeps_sender_columns_marked_for_collapse(db, client_logged_in):
    plan, sender, city, item = _setup()

    resp = client_logged_in.post(
        f"/shipment-plan/{plan.id}/cities/{city.id}/deadline",
        data={"ship_by_date": (date.today() + timedelta(days=2)).isoformat()},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert '<td class="sender-col">' in resp.get_data(as_text=True)
