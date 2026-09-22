from datetime import datetime

from wms.extensions import db
from wms.models import MovementDocument, User, Warehouse


def test_admin_sees_visual_management_dashboard(client_logged_in):
    response = client_logged_in.get("/management/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Пульс компании" in html
    assert "Сквозной процесс" in html
    assert "Скорость логистики" in html
    assert "Google Таблицы" in html
    assert "<table" not in html


def test_management_dashboard_requires_separate_permission(db, client):
    user = User(username="manager-test", full_name="Руководитель")
    user.set_password("manager-password")
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True

    denied = client.get("/management/")
    assert denied.status_code == 302

    user.management_dashboard_allowed = True
    db.session.commit()
    allowed = client.get("/management/")
    assert allowed.status_code == 200


def test_transport_pickup_is_separate_from_marketplace_receipt(db, client_logged_in):
    sender = Warehouse(code="MGMT-S", name="Основной")
    destination = Warehouse(
        code="MGMT-D", name="Казань", marketplace="wb", marketplace_city="Казань"
    )
    db.session.add_all([sender, destination])
    db.session.commit()
    document = MovementDocument(
        number="PER-MGMT-1",
        from_warehouse_id=sender.id,
        to_warehouse_id=destination.id,
        status="completed",
        completed_at=datetime.utcnow(),
        marketplace_request_number="REQ-MGMT-1",
        marketplace_request_created_at=datetime.utcnow(),
    )
    db.session.add(document)
    db.session.commit()

    response = client_logged_in.post(f"/movement/{document.id}/mark-shipped")

    assert response.status_code == 302
    document = MovementDocument.query.get(document.id)
    assert document.shipped_at is not None
    assert document.received_at is None

