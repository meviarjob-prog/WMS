"""Дашборд «Выработка сотрудников»: журнал упаковки/сборки (WorkEvent),
планы (WorkPlan) и страница reports.employee_output."""

from datetime import date, datetime

from wms.extensions import db
from wms.models import (
    Box, BoxItem, MovementDocument, MovementLine, Nomenclature, PlacementDocument,
    UnplacedStock, User, Warehouse, WorkEvent, WorkPlan,
)
from wms.utils.work_log import work_day


def _payload(client, query=""):
    import json
    import re

    html = client.get("/production/employee-output" + query).get_data(as_text=True)
    return json.loads(re.search(r'<script type="application/json" id="eo-data">(.*?)</script>', html, re.S).group(1))


def _warehouses():
    sender = Warehouse(code="WH-EO1", name="Основной")
    dest = Warehouse(code="WH-EO2", name="Склад МП", marketplace="wb", marketplace_city="Коледино")
    db.session.add_all([sender, dest])
    db.session.commit()
    return sender, dest


def _item(suffix="1"):
    item = Nomenclature(sku=f"EO-{suffix}", barcode=f"777000{suffix}", name=f"Шапка {suffix}", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def test_packing_into_box_via_placement_is_logged_per_user_and_day(db, client_logged_in, admin_user):
    sender, _ = _warehouses()
    item = _item()
    UnplacedStock.add(sender.id, item.id, 50)
    doc = PlacementDocument(number="PL-EO1", warehouse_id=sender.id)
    box = Box(box_number="BOX-EO-1", warehouse_id=sender.id, status="open")
    db.session.add_all([doc, box])
    db.session.commit()

    for qty in (10, 5):
        resp = client_logged_in.post(
            f"/placement/{doc.id}/boxes/{box.id}/items/add-by-barcode",
            json={"barcode": item.barcode, "qty": qty},
        )
        assert resp.status_code == 200

    events = WorkEvent.query.filter_by(kind="pack").all()
    assert [(e.user_id, e.work_date, e.qty, e.box_id) for e in events] == [
        (admin_user.id, work_day(), 10, box.id), (admin_user.id, work_day(), 5, box.id),
    ]


def test_picking_box_into_movement_is_logged_and_dropped_on_removal(db, client_logged_in, admin_user):
    sender, dest = _warehouses()
    item = _item()
    box = Box(box_number="BOX-EO-2", warehouse_id=sender.id, status="open")
    doc = MovementDocument(number="PER-EO1", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add_all([box, doc])
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=40))
    db.session.commit()

    client_logged_in.post(f"/movement/{doc.id}/boxes/add", data={"box_number": box.box_number})
    picks = WorkEvent.query.filter_by(kind="pick").all()
    assert len(picks) == 1 and picks[0].qty == 40 and picks[0].ref_id == doc.id

    line = MovementLine.query.filter_by(document_id=doc.id).first()
    client_logged_in.post(f"/movement/{doc.id}/lines/{line.id}/delete")
    assert WorkEvent.query.filter_by(kind="pick").count() == 0


def test_dashboard_aggregates_facts_and_applies_plans(db, client_logged_in, admin_user):
    today = work_day()
    worker = User(username="packer1", full_name="Иванова Анна", role="user")
    worker.set_password("x")
    db.session.add(worker)
    db.session.commit()
    for box_id, qty in ((1, 30), (1, 20), (2, 50)):
        db.session.add(WorkEvent(user_id=worker.id, work_date=today, kind="pack", box_id=box_id, qty=qty))
    db.session.add(WorkEvent(user_id=worker.id, work_date=today, kind="pick", box_id=9, qty=12))
    db.session.add(WorkPlan(user_id=worker.id, kind="pack", plan_date=None, qty_plan=200, boxes_plan=4))
    db.session.commit()

    uid = str(worker.id)
    data = _payload(client_logged_in)
    assert data["data"]["pack"][uid][today.isoformat()] == [100.0, 2]   # 30+20+50 шт в 2 коробах
    assert data["data"]["pick"][uid][today.isoformat()] == [12.0, 1]
    assert data["plans"]["pack"][uid]["def"] == [200.0, 4]

    # план на конкретную дату перекрывает план по умолчанию
    client_logged_in.post("/production/employee-output/plans", data={
        f"day__pack__{worker.id}__{today.isoformat()}__qty": "100",
        f"day__pack__{worker.id}__{today.isoformat()}__boxes": "",
        f"def__pick__{worker.id}__boxes": "3",
    })
    override = WorkPlan.query.filter_by(user_id=worker.id, kind="pack", plan_date=today).first()
    assert override.qty_plan == 100 and override.boxes_plan is None
    assert WorkPlan.query.filter_by(user_id=worker.id, kind="pick", plan_date=None).first().boxes_plan == 3
    data = _payload(client_logged_in)
    assert data["plans"]["pack"][uid]["days"][today.isoformat()] == [100.0, None]
    assert data["plans"]["pick"][uid]["def"] == [None, 3]


def test_one_day_plan_form_sets_and_clears_plan(db, client_logged_in, admin_user):
    worker = User(username="oneday1", full_name="Дневной", role="user")
    worker.set_password("x")
    db.session.add(worker)
    db.session.commit()
    day = "2026-09-10"
    client_logged_in.post("/production/employee-output/plans", data={
        "one_user": str(worker.id), "one_kind": "pick", "one_date": day, "one_qty": "", "one_boxes": "25",
    })
    plan = WorkPlan.query.filter_by(user_id=worker.id, kind="pick", plan_date=date(2026, 9, 10)).first()
    assert plan.boxes_plan == 25
    client_logged_in.post("/production/employee-output/plans", data={
        "one_user": str(worker.id), "one_kind": "pick", "one_date": day, "one_qty": "", "one_boxes": "",
    })
    assert WorkPlan.query.count() == 0


def test_blank_plan_removes_it_and_only_admin_can_edit(db, client_logged_in, admin_user):
    worker = User(username="picker1", full_name="Петров", role="user")
    worker.set_password("x")
    db.session.add(worker)
    db.session.commit()
    db.session.add(WorkPlan(user_id=worker.id, kind="pick", plan_date=None, boxes_plan=5))
    db.session.commit()

    client_logged_in.post("/production/employee-output/plans", data={f"def__pick__{worker.id}__boxes": ""})
    assert WorkPlan.query.count() == 0


def test_non_admin_cannot_change_plans(db, client):
    worker = User(username="viewer1", full_name="Смотрящий", role="user")
    worker.set_password("pw12345")
    db.session.add(worker)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(worker.id)
        sess["_fresh"] = True
    resp = client.post("/production/employee-output/plans", data={f"def__pick__{worker.id}__boxes": "9"})
    assert resp.status_code == 302
    assert WorkPlan.query.count() == 0


def test_dashboard_date_filter_and_user_filter(db, client_logged_in, admin_user):
    a = User(username="ua", full_name="Анна", role="user"); a.set_password("x")
    b = User(username="ub", full_name="Борис", role="user"); b.set_password("x")
    db.session.add_all([a, b])
    db.session.commit()
    day = date(2026, 9, 1)
    db.session.add_all([
        WorkEvent(user_id=a.id, work_date=day, kind="pack", box_id=1, qty=7),
        WorkEvent(user_id=b.id, work_date=day, kind="pack", box_id=2, qty=9),
    ])
    db.session.commit()
    data = _payload(client_logged_in, "?date_from=2026-09-01&date_to=2026-09-02")
    assert set(data["data"]["pack"]) == {str(a.id), str(b.id)}
    data = _payload(client_logged_in, f"?date_from=2026-09-01&date_to=2026-09-02&user_id={a.id}")
    assert set(data["data"]["pack"]) == {str(a.id)}
    assert data["data"]["pack"][str(a.id)]["2026-09-01"] == [7.0, 1]
    data = _payload(client_logged_in, "?date_from=2026-09-05&date_to=2026-09-06")
    assert data["data"]["pack"] == {} and data["data"]["pick"] == {}


def test_dashboard_excludes_mvb_logistics_users(db, client_logged_in, admin_user):
    worker = User(username="wh1", full_name="Складской Сергей", role="warehouse")
    client = User(username="cl1", full_name="Клиент Логистики", role="mvb_client")
    driver = User(username="dr1", full_name="Водитель Виктор", role="mvb_driver")
    for u in (worker, client, driver):
        u.set_password("x")
    db.session.add_all([worker, client, driver])
    db.session.commit()
    today = work_day()
    for u in (worker, client, driver):
        db.session.add(WorkEvent(user_id=u.id, work_date=today, kind="pick", box_id=u.id, qty=1))
    db.session.commit()
    html = client_logged_in.get("/production/employee-output").get_data(as_text=True)
    assert "Складской Сергей" in html
    assert "Клиент Логистики" not in html and "Водитель Виктор" not in html


def test_dashboard_page_embeds_chart_data_for_selected_period(db, client_logged_in, admin_user):
    import json
    import re

    worker = User(username="chart1", full_name="Графикова", role="warehouse")
    worker.set_password("x")
    db.session.add(worker)
    db.session.commit()
    db.session.add_all([
        WorkEvent(user_id=worker.id, work_date=date(2026, 9, 2), kind="pack", box_id=1, qty=30),
        WorkEvent(user_id=worker.id, work_date=date(2026, 9, 2), kind="pack", box_id=2, qty=20),
        WorkEvent(user_id=worker.id, work_date=date(2026, 9, 3), kind="pick", box_id=3, qty=40),
        WorkPlan(user_id=worker.id, kind="pack", plan_date=None, qty_plan=100, boxes_plan=3),
    ])
    db.session.commit()
    html = client_logged_in.get("/production/employee-output?date_from=2026-09-01&date_to=2026-09-03").get_data(as_text=True)
    data = json.loads(re.search(r'<script type="application/json" id="eo-data">(.*?)</script>', html, re.S).group(1))
    assert data["dates"] == ["2026-09-01", "2026-09-02", "2026-09-03"]
    uid = str(worker.id)
    assert data["data"]["pack"][uid]["2026-09-02"] == [50.0, 2]
    assert data["data"]["pick"][uid]["2026-09-03"] == [40.0, 1]
    assert data["plans"]["pack"][uid]["def"] == [100.0, 3]
    assert data["users"][uid] == "Графикова"


def test_backfill_restores_history_from_document_authors_and_is_idempotent(db, client_logged_in, admin_user):
    from wms.models import PlacementLine, ReceivingDocument, ReceivingLine

    sender, dest = _warehouses()
    item = _item("9")
    author = User(username="hist1", full_name="Историк", role="warehouse")
    author.set_password("x")
    db.session.add(author)
    box_a = Box(box_number="BOX-H-1", warehouse_id=sender.id, status="open")
    box_b = Box(box_number="BOX-H-2", warehouse_id=sender.id, status="open")
    db.session.add_all([box_a, box_b])
    db.session.commit()
    db.session.add(BoxItem(box_id=box_a.id, nomenclature_id=item.id, qty=12))

    old = datetime(2026, 8, 10, 7, 0)
    rdoc = ReceivingDocument(number="RC-H1", warehouse_id=sender.id, created_by_id=author.id, created_at=old)
    pdoc = PlacementDocument(number="PL-H1", warehouse_id=sender.id, created_by_id=author.id, created_at=old)
    mdoc = MovementDocument(number="PER-H1", from_warehouse_id=sender.id, to_warehouse_id=dest.id, created_by_id=author.id)
    db.session.add_all([rdoc, pdoc, mdoc])
    db.session.commit()
    db.session.add_all([
        ReceivingLine(document_id=rdoc.id, nomenclature_id=item.id, qty=7, box_id=box_a.id),
        ReceivingLine(document_id=rdoc.id, nomenclature_id=item.id, qty=3),  # без короба — не упаковка
        PlacementLine(document_id=pdoc.id, nomenclature_id=item.id, qty=5, box_id=box_b.id),
        MovementLine(document_id=mdoc.id, box_id=box_a.id, from_warehouse_id=sender.id, scanned_at=old),
    ])
    db.session.commit()

    client_logged_in.post("/production/employee-output/backfill")
    client_logged_in.post("/production/employee-output/backfill")  # повтор не задваивает
    events = WorkEvent.query.filter_by(ref_kind="history").all()
    assert sorted((e.kind, e.qty) for e in events) == [("pack", 5), ("pack", 7), ("pick", 12)]
    assert {e.user_id for e in events} == {author.id}
    assert {e.work_date for e in events} == {date(2026, 8, 10)}

    html = client_logged_in.get("/production/employee-output?date_from=2026-08-01&date_to=2026-08-31").get_data(as_text=True)
    assert "восстановленные (приблизительные) данные" in html


def test_backfill_keeps_real_events_and_skips_period_after_them(db, client_logged_in, admin_user):
    from wms.models import ReceivingDocument, ReceivingLine

    sender, _ = _warehouses()
    item = _item("8")
    box = Box(box_number="BOX-H-3", warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    real = WorkEvent(user_id=admin_user.id, work_date=work_day(), kind="pack", box_id=box.id, qty=1)
    db.session.add(real)
    db.session.commit()
    # документ, созданный ПОСЛЕ начала настоящего учёта, не восстанавливается
    doc = ReceivingDocument(number="RC-H2", warehouse_id=sender.id, created_by_id=admin_user.id,
                            created_at=datetime.utcnow())
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=9, box_id=box.id))
    db.session.commit()

    client_logged_in.post("/production/employee-output/backfill")
    assert WorkEvent.query.filter_by(ref_kind="history").count() == 0
    assert WorkEvent.query.filter(WorkEvent.ref_kind.is_(None)).count() == 1


def test_backfill_requires_admin(db, client):
    worker = User(username="nb1", full_name="Не админ", role="warehouse")
    worker.set_password("pw12345")
    db.session.add(worker)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(worker.id)
        sess["_fresh"] = True
    client.post("/production/employee-output/backfill")
    assert WorkEvent.query.count() == 0


def test_old_reports_url_redirects_to_production_section(db, client_logged_in):
    resp = client_logged_in.get("/reports/employee-output?date_from=2026-09-01&date_to=2026-09-03")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/production/employee-output")
    assert "date_from=2026-09-01" in resp.headers["Location"]


def test_production_index_links_to_dashboard_and_reports_index_does_not(db, client_logged_in):
    assert "Выработка сотрудников" in client_logged_in.get("/production/").get_data(as_text=True)
    assert "employee-output" not in client_logged_in.get("/reports/").get_data(as_text=True)


def test_production_only_worker_cannot_open_dashboard(db, client):
    worker = User(username="prod_only", full_name="Производственник", role="production")
    worker.set_password("pw12345")
    db.session.add(worker)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(worker.id)
        sess["_fresh"] = True
    resp = client.get("/production/employee-output")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/production/")
    assert "Выработка сотрудников" not in client.get("/production/").get_data(as_text=True)
