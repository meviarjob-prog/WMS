"""«Куда везти короб»: рекомендация склада-города по остатку невыполненной
потребности плана отгрузок, с учетом уже едущих туда (но не принятых)
коробов — см. обсуждение "нужно 30, отсканировали короб с 10"."""

from datetime import date, datetime, timedelta

from flask import g

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    Nomenclature,
    ShipmentPlan,
    ShipmentPlanLine,
    User,
    Warehouse,
)
from wms.blueprints.movement import _compute_routing


def _login_as(client, user):
    # Тест держит один app context на весь запуск (см. фикстуру db) — без
    # сброса кэша Flask-Login "current_user" следующий запрос тем же
    # client все еще резолвился бы в ПРЕДЫДУЩЕГО пользователя (см. тот же
    # прием в test_auth_session_revocation.py).
    g.pop("_login_user", None)
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _make_staff_user(username):
    user = User(username=username, full_name=username, role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    return user


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
    assert box.box_number in html
    assert "Покрывает" not in html
    assert "Нужно всего" not in html
    assert "Содержимое короба" in html
    assert "Товар" in html
    assert "30 шт." not in html
    assert html.index("Содержимое короба") < html.index("ОЗОН: Город")


def test_routing_subtracts_already_committed_boxes(db, client_logged_in):
    """Пример из обсуждения: нужно 30, короб с 10 уже добавлен в
    перемещение (черновик) — следующий скан должен увидеть остаток 20."""
    sender, city, item = _setup_plan(planned_qty=30)
    box1 = _make_box(sender, item, qty=10, box_number="BOX-000001")
    box2 = _make_box(sender, item, qty=5, box_number="BOX-000002")

    client_logged_in.post(
        "/movement/route-box/add", data={"box_id": box1.id, "to_warehouse_id": city.id}
    )

    routing = _compute_routing(box2)
    assert routing[0]["total_remaining"] == 20


def test_routing_subtracts_boxes_already_in_transit(db, client_logged_in):
    """Тот же принцип, что и для черновика, но короб уже физически уехал
    (перемещение завершено), просто еще не подтвержден кнопкой "Принято на
    складе" — такой короб тоже должен считаться "закрывающим" потребность
    при расчете подсказки для следующего короба, иначе подсказка отправит
    туда больше, чем реально нужно."""
    sender, city, item = _setup_plan(planned_qty=30)
    box1 = _make_box(sender, item, qty=10, box_number="BOX-000001")
    box2 = _make_box(sender, item, qty=5, box_number="BOX-000002")

    client_logged_in.post(
        "/movement/route-box/add", data={"box_id": box1.id, "to_warehouse_id": city.id}
    )
    doc = MovementDocument.query.filter_by(from_warehouse_id=sender.id, to_warehouse_id=city.id).first()
    client_logged_in.post(f"/movement/{doc.id}/complete")
    doc = MovementDocument.query.get(doc.id)
    assert doc.status == "completed"
    assert doc.received_at is None  # именно "в пути", не принято

    routing = _compute_routing(box2)
    assert routing[0]["total_remaining"] == 20


def test_routing_does_not_carry_old_boxes_into_new_dated_plan(db, client_logged_in):
    """После смены даты листа это новый план: короб, отсканированный в
    перемещение до новой даты, не уменьшает новую потребность."""
    sender, city, item = _setup_plan(planned_qty=30)
    plan = ShipmentPlan.query.filter_by(marketplace="ozon").first()
    plan.period_start = date.today()
    line = ShipmentPlanLine.query.filter_by(plan_id=plan.id).first()
    line.period_start = date.today()

    old_box = _make_box(sender, item, qty=10, box_number="BOX-OLD-PLAN")
    new_box = _make_box(sender, item, qty=5, box_number="BOX-NEW-PLAN")
    doc = MovementDocument(
        number="MOV-OLD-PLAN",
        from_warehouse_id=sender.id,
        to_warehouse_id=city.id,
        status="draft",
    )
    db.session.add(doc)
    db.session.flush()
    db.session.add(
        MovementLine(
            document_id=doc.id,
            box_id=old_box.id,
            from_warehouse_id=sender.id,
            scanned_at=datetime.combine(date.today() - timedelta(days=1), datetime.min.time()),
        )
    )
    db.session.commit()

    routing = _compute_routing(new_box)
    assert routing[0]["total_remaining"] == 30


def test_routing_add_stays_on_scanning_page_not_document(db, client_logged_in):
    """Сборщик сканирует короба один за другим и не должен всякий раз
    улетать внутрь документа перемещения — иначе процесс распределения
    коробов постоянно прерывается."""
    sender, city, item = _setup_plan(planned_qty=30)
    box = _make_box(sender, item, qty=10, box_number="BOX-000001")

    resp = client_logged_in.post(
        "/movement/route-box/add",
        data={"box_id": box.id, "to_warehouse_id": city.id},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].rstrip("/") == "/movement"


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


def test_route_box_add_joins_draft_started_by_a_different_user(db, client):
    """Регрессия: route_box_add искал существующий черновик через
    owned_query (только СВОИ документы), поэтому второй сотрудник,
    собирающий то же направление, не находил черновик первого и получал
    отдельный документ — то есть двое собирали одно направление в двух
    разных документах вместо одного общего. Тесты с client_logged_in
    (админ) это не ловили: owned_query не ограничивает админа."""
    sender, city, item = _setup_plan(planned_qty=30)
    box1 = _make_box(sender, item, qty=10, box_number="BOX-000020")
    box2 = _make_box(sender, item, qty=5, box_number="BOX-000021")

    alice = _make_staff_user("alice-routing")
    bob = _make_staff_user("bob-routing")

    _login_as(client, alice)
    client.post("/movement/route-box/add", data={"box_id": box1.id, "to_warehouse_id": city.id})

    _login_as(client, bob)
    client.post("/movement/route-box/add", data={"box_id": box2.id, "to_warehouse_id": city.id})

    docs = MovementDocument.query.filter_by(from_warehouse_id=sender.id, to_warehouse_id=city.id).all()
    assert len(docs) == 1
    assert docs[0].lines.count() == 2


def test_route_box_add_blocks_box_already_in_other_draft_movement(db, client_logged_in):
    """Один короб нельзя одновременно отсканировать сразу в два разных
    черновика перемещения — пока черновик не завершен, warehouse_id короба
    не меняется, поэтому такое задвоение раньше проходило молча."""
    sender, city, item = _setup_plan(planned_qty=30)
    other_city = Warehouse(
        code="WH-CTY2", name="ОЗОН: Другой город", marketplace="ozon", marketplace_city="Другой город"
    )
    db.session.add(other_city)
    db.session.commit()
    box = _make_box(sender, item, qty=10, box_number="BOX-000009")

    client_logged_in.post("/movement/route-box/add", data={"box_id": box.id, "to_warehouse_id": city.id})

    resp = client_logged_in.post(
        "/movement/route-box/add",
        data={"box_id": box.id, "to_warehouse_id": other_city.id},
        follow_redirects=True,
    )

    html = resp.get_data(as_text=True)
    assert "уже отсканирован в другое перемещение" in html
    assert MovementLine.query.filter_by(box_id=box.id).count() == 1


def test_add_box_blocks_box_already_in_other_draft_movement(db, client_logged_in):
    """Та же блокировка при добавлении короба вручную на странице
    конкретного документа перемещения (не только через «Куда везти
    короб»)."""
    sender, city, item = _setup_plan(planned_qty=30)
    box = _make_box(sender, item, qty=10, box_number="BOX-000010")

    client_logged_in.post(
        "/movement/new", data={"from_warehouse_id": sender.id, "to_warehouse_id": city.id}
    )
    doc1 = MovementDocument.query.filter_by(from_warehouse_id=sender.id, to_warehouse_id=city.id).first()
    client_logged_in.post(f"/movement/{doc1.id}/boxes/add", data={"box_number": box.box_number})
    assert doc1.lines.count() == 1

    other_city = Warehouse(code="WH-CTY3", name="Другой склад назначения")
    db.session.add(other_city)
    db.session.commit()
    client_logged_in.post(
        "/movement/new", data={"from_warehouse_id": sender.id, "to_warehouse_id": other_city.id}
    )
    doc2 = MovementDocument.query.filter_by(
        from_warehouse_id=sender.id, to_warehouse_id=other_city.id
    ).first()

    resp = client_logged_in.post(
        f"/movement/{doc2.id}/boxes/add", data={"box_number": box.box_number}, follow_redirects=True
    )

    html = resp.get_data(as_text=True)
    assert "уже отсканирован в другое перемещение" in html
    assert doc2.lines.count() == 0


def test_add_box_same_document_duplicate_message_unaffected(db, client_logged_in):
    """Повторное сканирование короба в ТОТ ЖЕ документ — это отдельный,
    уже существующий (и не блокирующий по смыслу теста) случай, который
    новая проверка не должна затронуть."""
    sender, city, item = _setup_plan(planned_qty=30)
    box = _make_box(sender, item, qty=10, box_number="BOX-000011")

    client_logged_in.post(
        "/movement/new", data={"from_warehouse_id": sender.id, "to_warehouse_id": city.id}
    )
    doc = MovementDocument.query.filter_by(from_warehouse_id=sender.id, to_warehouse_id=city.id).first()
    client_logged_in.post(f"/movement/{doc.id}/boxes/add", data={"box_number": box.box_number})

    resp = client_logged_in.post(
        f"/movement/{doc.id}/boxes/add", data={"box_number": box.box_number}, follow_redirects=True
    )

    html = resp.get_data(as_text=True)
    assert "уже в этом списке" in html
    assert doc.lines.count() == 1


def test_route_box_add_blocks_box_already_in_transit_completed_movement(db, client_logged_in):
    """Короб, чье первое перемещение уже "Завершено" (и, значит,
    box.warehouse_id уже переставлен на склад назначения), но еще НЕ
    отмечено "Принято на складе" — то есть физически еще в пути — раньше
    можно было молча добавить в ЕЩЕ ОДНО перемещение через "Куда везти
    короб": склад-отправитель нового документа берется из текущего
    (уже переставленного) box.warehouse_id, поэтому проверка "короб не на
    складе-отправителе" не срабатывала, а _find_conflicting_movement_line
    смотрела только на черновики. Один физический короб оказывался
    одновременно "отправлен" в два разных города."""
    sender, city, item = _setup_plan(planned_qty=30)
    other_city = Warehouse(
        code="WH-CTY4", name="ОЗОН: Третий город", marketplace="ozon", marketplace_city="Третий город"
    )
    db.session.add(other_city)
    db.session.commit()
    box = _make_box(sender, item, qty=10, box_number="BOX-000012")

    client_logged_in.post("/movement/route-box/add", data={"box_id": box.id, "to_warehouse_id": city.id})
    doc1 = MovementDocument.query.filter_by(from_warehouse_id=sender.id, to_warehouse_id=city.id).first()
    client_logged_in.post(f"/movement/{doc1.id}/complete")
    doc1 = MovementDocument.query.get(doc1.id)
    assert doc1.status == "completed"
    assert doc1.received_at is None

    resp = client_logged_in.post(
        "/movement/route-box/add",
        data={"box_id": box.id, "to_warehouse_id": other_city.id},
        follow_redirects=True,
    )

    html = resp.get_data(as_text=True)
    assert "уже отсканирован в другое перемещение" in html
    assert "в пути" in html
    assert MovementLine.query.filter_by(box_id=box.id).count() == 1
