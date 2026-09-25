from datetime import date

from wms.extensions import db
from wms.models import TradeCustomer, TradeOrder, TradeVisit, User


def _login(client, user):
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def _representative():
    user = User(
        username="sinana-rep",
        full_name="Елена Смирнова",
        role="warehouse",
        allowed_sections="trade",
    )
    user.set_password("representative-password")
    db.session.add(user)
    db.session.commit()
    return user


def test_admin_can_create_customer_and_schedule_route(db, client_logged_in):
    representative = _representative()

    response = client_logged_in.post(
        "/trade/setup/customer",
        data={
            "name": "Супермаркет Ромашка",
            "address": "ул. Центральная, 8",
            "representative_id": representative.id,
            "credit_limit": "50000",
        },
    )
    customer = TradeCustomer.query.one()
    assert response.status_code == 302
    assert customer.assigned_rep_id == representative.id

    response = client_logged_in.post(
        "/trade/setup/visit",
        data={"customer_id": customer.id, "visit_date": date.today().isoformat()},
    )

    assert response.status_code == 302
    visit = TradeVisit.query.one()
    assert visit.representative_id == representative.id
    assert visit.sequence == 1


def test_representative_completes_visit_with_idempotent_order(db, client):
    representative = _representative()
    customer = TradeCustomer(
        name="Магазин У дома",
        address="ул. Гагарина, 14",
        assigned_rep_id=representative.id,
    )
    db.session.add(customer)
    db.session.flush()
    visit = TradeVisit(
        visit_date=date.today(),
        sequence=1,
        customer_id=customer.id,
        representative_id=representative.id,
    )
    db.session.add(visit)
    db.session.commit()
    _login(client, representative)

    mobile = client.get("/trade/mobile")
    assert mobile.status_code == 200
    assert "Магазин У дома" in mobile.get_data(as_text=True)
    assert "SINANA" in mobile.get_data(as_text=True)

    started = client.post(f"/trade/visits/{visit.id}/start")
    assert started.status_code == 302
    assert TradeVisit.query.get(visit.id).status == "in_progress"

    payload = {
        "submission_token": "stable-mobile-token",
        "total_amount": "18600",
        "comment": "Доставка завтра",
    }
    created = client.post(f"/trade/customers/{customer.id}/order", data=payload)
    repeated = client.post(f"/trade/customers/{customer.id}/order", data=payload)

    assert created.status_code == 302
    assert repeated.status_code == 302
    assert TradeOrder.query.count() == 1
    order = TradeOrder.query.one()
    assert order.total_amount == 18600
    assert order.number.startswith("ZAK-")
    assert TradeVisit.query.get(visit.id).result == "order"


def test_trade_dashboard_uses_real_sales_data(db, client_logged_in, admin_user):
    customer = TradeCustomer(name="Кафе Ваниль", address="ул. Молодежная, 19")
    db.session.add(customer)
    db.session.flush()
    db.session.add(
        TradeOrder(
            number="ZAK-TEST-1",
            customer_id=customer.id,
            representative_id=admin_user.id,
            status="submitted",
            total_amount=42100,
        )
    )
    db.session.commit()

    response = client_logged_in.get("/trade/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "SINANA" in html
    assert "42 100 ₽" in html
    assert "Кафе Ваниль" in html


def test_non_manager_is_sent_to_mobile_trade_workspace(db, client):
    representative = _representative()
    _login(client, representative)

    response = client.get("/trade/")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/trade/mobile")
