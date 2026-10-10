"""Роль "фулфилмент" (см. чат): видит и ведет только приемки и
перемещения своего склада (warehouse_id) — ничего больше в WMS (другие
разделы, другие склады). Остатки в номенклатуре/приемке учитывают новый
физический склад "ЦЕХ Марат" как третий (см. models.PHYSICAL_STOCK_WAREHOUSE_NAMES)."""

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    Nomenclature,
    ReceivingDocument,
    User,
    Warehouse,
)


def _warehouse(code, name="Основной"):
    wh = Warehouse(code=code, name=name)
    db.session.add(wh)
    db.session.commit()
    return wh


def _fulfillment(username, warehouse, suffix=""):
    user = User(username=username, is_admin=False, role="fulfillment", warehouse_id=warehouse.id)
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_is_fulfillment_only_true_only_for_non_admin():
    wh = Warehouse(id=1, code="X", name="X")
    assert User(role="fulfillment", is_admin=False).is_fulfillment_only() is True
    assert User(role="fulfillment", is_admin=True).is_fulfillment_only() is False
    assert User(role="warehouse", is_admin=False).is_fulfillment_only() is False


def test_create_user_fulfillment_requires_warehouse(client_logged_in, db):
    resp = client_logged_in.post("/users/create", data={"username": "no-wh-fulfillment", "role": "fulfillment"})
    assert resp.status_code == 302
    assert User.query.filter_by(username="no-wh-fulfillment").first() is None


def test_create_user_fulfillment_sets_allowed_sections(client_logged_in, db):
    wh = _warehouse("WH-FF-CREATE", "ЦЕХ Марат")
    resp = client_logged_in.post(
        "/users/create", data={"username": "new-fulfillment", "role": "fulfillment", "warehouse_id": wh.id}
    )
    assert resp.status_code == 302
    created = User.query.filter_by(username="new-fulfillment").first()
    assert created is not None
    assert created.role == "fulfillment"
    assert created.warehouse_id == wh.id
    assert created.allowed_sections == "receiving,movement"


def test_update_role_to_fulfillment_requires_warehouse(client_logged_in, db):
    target = User(username="switch-to-fulfillment", is_admin=False, role="warehouse")
    target.set_password("password123")
    db.session.add(target)
    db.session.commit()

    resp = client_logged_in.post(f"/users/{target.id}/role", data={"role": "fulfillment"})

    assert resp.status_code == 302
    assert User.query.get(target.id).role == "warehouse"  # не изменилось — нет склада

    wh = _warehouse("WH-FF-UPD", "ЦЕХ Марат 2")
    target.warehouse_id = wh.id
    db.session.commit()
    resp = client_logged_in.post(f"/users/{target.id}/role", data={"role": "fulfillment"})
    assert resp.status_code == 302
    updated = User.query.get(target.id)
    assert updated.role == "fulfillment"
    assert updated.allowed_sections == "receiving,movement"


def test_fulfillment_sees_only_own_warehouse_sections_in_menu(db, client):
    wh = _warehouse("WH-FF-MENU", "ЦЕХ Марат")
    user = _fulfillment("ff-menu", wh)
    _login(client, user)

    html = client.get("/receiving/").get_data(as_text=True)
    nav = html.split("</nav>")[0]
    assert "Приемка" in nav
    assert "Перемещение" in nav
    assert "Номенклатура" not in nav
    assert "Размещение" not in nav
    assert "Инвентаризация" not in nav
    assert "Отчеты" not in nav


def test_fulfillment_blocked_from_other_sections_by_direct_url(db, client):
    wh = _warehouse("WH-FF-DIRECT", "ЦЕХ Марат")
    user = _fulfillment("ff-direct", wh)
    _login(client, user)

    for path in ("/nomenclature/", "/placement/", "/inventory/", "/warehouses/", "/reports/"):
        resp = client.get(path)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/")


def test_fulfillment_sees_receiving_of_own_warehouse_from_any_author(db, client):
    wh = _warehouse("WH-FF-RCV", "ЦЕХ Марат")
    other_wh = _warehouse("WH-FF-RCV-OTHER", "Другой склад")
    user = _fulfillment("ff-rcv", wh)
    colleague = User(username="ff-rcv-colleague", is_admin=False, role="fulfillment", warehouse_id=wh.id)
    colleague.set_password("password123")
    db.session.add(colleague)
    db.session.commit()

    own = ReceivingDocument(number="RCV-FF-1", warehouse_id=wh.id, created_by_id=colleague.id)
    foreign = ReceivingDocument(number="RCV-FF-2", warehouse_id=other_wh.id, created_by_id=colleague.id)
    db.session.add_all([own, foreign])
    db.session.commit()

    _login(client, user)
    html = client.get("/receiving/").get_data(as_text=True)
    assert "RCV-FF-1" in html
    assert "RCV-FF-2" not in html

    assert client.get(f"/receiving/{own.id}").status_code == 200
    assert client.get(f"/receiving/{foreign.id}").status_code == 404


def test_fulfillment_can_only_create_receiving_for_own_warehouse(db, client):
    wh = _warehouse("WH-FF-NEW", "ЦЕХ Марат")
    _warehouse("WH-FF-NEW-OTHER", "основной")
    user = _fulfillment("ff-new", wh)
    _login(client, user)

    html = client.get("/receiving/new").get_data(as_text=True)
    assert "ЦЕХ Марат" in html
    assert "основной" not in html.lower().replace("основной склад", "")

    resp = client.post("/receiving/new", data={"warehouse_id": wh.id, "supplier": ""}, follow_redirects=True)
    assert resp.status_code == 200
    doc = ReceivingDocument.query.filter_by(number=ReceivingDocument.query.order_by(ReceivingDocument.id.desc()).first().number).first()
    assert doc.warehouse_id == wh.id


def test_fulfillment_sees_movements_from_own_warehouse_from_any_author(db, client):
    wh = _warehouse("WH-FF-MV", "ЦЕХ Марат")
    dest = _warehouse("WH-FF-MV-DEST", "Казань")
    other_wh = _warehouse("WH-FF-MV-OTHER", "Другой склад")
    user = _fulfillment("ff-mv", wh)
    colleague = User(username="ff-mv-colleague", is_admin=False, role="fulfillment", warehouse_id=wh.id)
    colleague.set_password("password123")
    db.session.add(colleague)
    db.session.commit()

    own = MovementDocument(number="MV-FF-1", from_warehouse_id=wh.id, to_warehouse_id=dest.id, created_by_id=colleague.id, status="completed")
    incoming = MovementDocument(number="MV-FF-2", from_warehouse_id=other_wh.id, to_warehouse_id=wh.id, created_by_id=colleague.id, status="completed")
    foreign = MovementDocument(number="MV-FF-3", from_warehouse_id=other_wh.id, to_warehouse_id=dest.id, created_by_id=colleague.id, status="completed")
    db.session.add_all([own, incoming, foreign])
    db.session.commit()

    _login(client, user)
    html = client.get("/movement/").get_data(as_text=True)
    assert "MV-FF-1" in html
    assert "MV-FF-2" not in html  # входящие (to_warehouse) фулфилмент не видит
    assert "MV-FF-3" not in html

    assert client.get(f"/movement/{own.id}").status_code == 200
    assert client.get(f"/movement/{foreign.id}").status_code == 404


def test_fulfillment_can_complete_colleagues_movement_on_own_warehouse(db, client):
    wh = _warehouse("WH-FF-COMPLETE", "ЦЕХ Марат")
    dest = _warehouse("WH-FF-COMPLETE-DEST", "Казань")
    user = _fulfillment("ff-complete", wh)
    colleague = User(username="ff-complete-colleague", is_admin=False, role="fulfillment", warehouse_id=wh.id)
    colleague.set_password("password123")
    db.session.add(colleague)
    db.session.commit()

    item = Nomenclature(sku="FF-SKU-1", barcode="7779990001", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    box = Box(box_number="BOX-FF-1", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=2))
    doc = MovementDocument(number="MV-FF-COMPLETE", from_warehouse_id=wh.id, to_warehouse_id=dest.id, created_by_id=colleague.id, status="draft")
    db.session.add(doc)
    db.session.commit()
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=wh.id))
    db.session.commit()

    _login(client, user)
    resp = client.post(f"/movement/{doc.id}/complete")
    assert resp.status_code == 302
    assert MovementDocument.query.get(doc.id).status == "completed"


def test_stock_report_shows_tsekh_marat_as_third_warehouse(db, client_logged_in):
    wh = _warehouse("WH-TSEKH-STOCK", "ЦЕХ Марат")
    item = Nomenclature(sku="FF-STOCK-1", barcode="7779990002", name="Товар на цехе", unit="шт")
    db.session.add(item)
    db.session.commit()
    from wms.models import UnplacedStock

    UnplacedStock.add(wh.id, item.id, 5)

    html = client_logged_in.get("/nomenclature/").get_data(as_text=True)
    assert "ЦЕХ Марат" in html
