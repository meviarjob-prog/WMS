from datetime import datetime

from wms.extensions import db
from wms.models import MovementDocument, User, Warehouse


def _completed_movement(*, number="PER-QUICK-SHIPPED"):
    sender = Warehouse(code=f"{number}-S", name="Основной")
    destination = Warehouse(
        code=f"{number}-D",
        name="Казань",
        marketplace="wb",
        marketplace_city="Казань",
    )
    db.session.add_all([sender, destination])
    db.session.flush()
    document = MovementDocument(
        number=number,
        from_warehouse_id=sender.id,
        to_warehouse_id=destination.id,
        status="completed",
        completed_at=datetime.utcnow(),
        marketplace_request_number="REQ-QUICK-1",
        marketplace_request_created_at=datetime.utcnow(),
    )
    db.session.add(document)
    db.session.commit()
    return document


def test_admin_can_mark_and_unmark_shipped_from_list(db, client_logged_in):
    document = _completed_movement()

    marked = client_logged_in.post(f"/movement/{document.id}/toggle-shipped")

    assert marked.status_code == 200
    assert marked.get_json()["checked"] is True
    assert MovementDocument.query.get(document.id).shipped_at is not None

    unmarked = client_logged_in.post(f"/movement/{document.id}/toggle-shipped")

    assert unmarked.status_code == 200
    assert unmarked.get_json()["checked"] is False
    assert MovementDocument.query.get(document.id).shipped_at is None


def test_quick_shipped_requires_marketplace_request(db, client_logged_in):
    document = _completed_movement(number="PER-QUICK-NO-REQ")
    document.marketplace_request_number = None
    document.marketplace_request_created_at = None
    db.session.commit()

    response = client_logged_in.post(f"/movement/{document.id}/toggle-shipped")

    assert response.status_code == 400
    assert "заявки на МП" in response.get_json()["error"]
    assert MovementDocument.query.get(document.id).shipped_at is None


def test_quick_shipped_is_admin_only(db, client):
    document = _completed_movement(number="PER-QUICK-WORKER")
    worker = User(username="quick-worker", role="warehouse", is_admin=False)
    worker.set_password("password")
    db.session.add(worker)
    db.session.flush()
    document.created_by_id = worker.id
    db.session.commit()
    with client.session_transaction() as session:
        session["_user_id"] = str(worker.id)
        session["_fresh"] = True

    response = client.post(f"/movement/{document.id}/toggle-shipped")

    assert response.status_code == 403
    assert MovementDocument.query.get(document.id).shipped_at is None


def test_received_movement_cannot_be_changed_by_quick_flag(db, client_logged_in):
    document = _completed_movement(number="PER-QUICK-RECEIVED")
    document.shipped_at = datetime.utcnow()
    document.received_at = datetime.utcnow()
    db.session.commit()

    response = client_logged_in.post(f"/movement/{document.id}/toggle-shipped")

    assert response.status_code == 400
    assert "уже принят" in response.get_json()["error"]
    assert MovementDocument.query.get(document.id).shipped_at is not None


def test_admin_sees_quick_shipped_column_in_movement_list(db, client_logged_in):
    document = _completed_movement(number="PER-QUICK-LIST")

    response = client_logged_in.get("/movement/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Отгружено" in html
    assert f'/movement/{document.id}/toggle-shipped' in html
