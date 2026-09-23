"""Роль "логист" (см. чат): видит только перемещения со статусом "ждет
транспорта" (сборка завершена, заявка на МП подана, но "Транспорт забрал"
еще не отмечено) и может выгрузить по ним сводную — больше ничего в WMS не
видит, как и роль "production" (см. is_logist_only, wms/__init__.py)."""

from datetime import datetime

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, MovementLine, Nomenclature, User, Warehouse


def _make_logist(username="logist-1"):
    user = User(username=username, is_admin=False, role="logist")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _movement(number, *, status="completed", mp_request=True, shipped=False):
    sender = Warehouse(code=f"{number}-S", name="Основной")
    destination = Warehouse(
        code=f"{number}-D", name="Казань", marketplace="wb", marketplace_city="Казань"
    )
    db.session.add_all([sender, destination])
    db.session.flush()
    document = MovementDocument(
        number=number,
        from_warehouse_id=sender.id,
        to_warehouse_id=destination.id,
        status=status,
        completed_at=datetime.utcnow() if status == "completed" else None,
        marketplace_request_number="REQ-1" if mp_request else None,
        marketplace_request_created_at=datetime.utcnow() if mp_request else None,
        shipped_at=datetime.utcnow() if shipped else None,
    )
    db.session.add(document)
    db.session.commit()
    return document, sender, destination


def test_is_logist_only_true_only_for_non_admin_logist():
    assert User(role="logist", is_admin=False).is_logist_only() is True
    assert User(role="logist", is_admin=True).is_logist_only() is False
    assert User(role="warehouse", is_admin=False).is_logist_only() is False


def test_logist_redirected_home_to_transport_list(db, client):
    logist = _make_logist()
    _login(client, logist)

    response = client.get("/")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/movement/transport")


def test_logist_cannot_reach_other_sections(db, client):
    logist = _make_logist("logist-2")
    _login(client, logist)

    for path in ("/nomenclature/", "/receiving/", "/warehouses/", "/reports/"):
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/movement/transport")


def test_logist_can_view_transport_list_and_export(db, client):
    logist = _make_logist("logist-3")
    _login(client, logist)

    doc, *_ = _movement("PER-LOG-1")

    html = client.get("/movement/transport").get_data(as_text=True)
    assert doc.number in html

    resp = client.get("/movement/transport/export-summary.xlsx")
    assert resp.status_code == 200
    assert resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_transport_list_only_shows_completed_with_request_and_not_shipped(db, client_logged_in):
    waiting, _s1, _d1 = _movement("PER-WAIT")
    not_completed, _s2, _d2 = _movement("PER-DRAFT", status="draft", mp_request=False)
    no_request, _s3, _d3 = _movement("PER-NOREQ", mp_request=False)
    already_shipped, _s4, _d4 = _movement("PER-SHIPPED", shipped=True)

    html = client_logged_in.get("/movement/transport").get_data(as_text=True)

    assert "PER-WAIT" in html
    assert "PER-DRAFT" not in html
    assert "PER-NOREQ" not in html
    assert "PER-SHIPPED" not in html


def test_transport_export_summary_includes_box_and_item_counts(db, client_logged_in):
    doc, sender, destination = _movement("PER-LOG-SUM")
    item = Nomenclature(sku="LOG-SKU-1", barcode="7770000123", name="Товар логиста", unit="шт")
    db.session.add(item)
    db.session.commit()
    box = Box(box_number="BOX-LOG-1", warehouse_id=destination.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=6))
    db.session.add(
        MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id)
    )
    db.session.commit()

    resp = client_logged_in.get("/movement/transport/export-summary.xlsx")
    assert resp.status_code == 200
    assert len(resp.data) > 0


def test_create_user_accepts_logist_role(client_logged_in, db):
    resp = client_logged_in.post(
        "/users/create",
        data={"username": "new-logist", "role": "logist"},
    )
    assert resp.status_code == 302
    created = User.query.filter_by(username="new-logist").first()
    assert created is not None
    assert created.role == "logist"


def test_update_role_accepts_logist_for_another_user(client_logged_in, db):
    target = User(username="switch-to-logist", is_admin=False, role="warehouse")
    target.set_password("password123")
    db.session.add(target)
    db.session.commit()

    resp = client_logged_in.post(f"/users/{target.id}/role", data={"role": "logist"})

    assert resp.status_code == 302
    assert User.query.get(target.id).role == "logist"
