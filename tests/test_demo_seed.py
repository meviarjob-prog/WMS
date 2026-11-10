"""Демо-версия для презентации (wms/demo_data.py, run_demo.py): наполнение
вымышленными данными работает только в демо-режиме и не может засорить
боевую базу; после наполнения основные экраны открываются и показывают
осмысленные цифры."""

import pytest
from sqlalchemy.pool import StaticPool

from wms import create_app
from wms.config import Config
from wms.demo_data import DEMO_PASSWORD, seed_demo
from wms.models import (
    MovementDocument,
    Nomenclature,
    ProductionRecord,
    ShipmentPlan,
    ShipmentPlanCityDeadline,
    User,
)


class DemoTestConfig(Config):
    TESTING = True
    DEMO_MODE = True
    SECRET_KEY = "test-secret-key"
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SQLALCHEMY_ENGINE_OPTIONS = {
        "poolclass": StaticPool,
        "connect_args": {"check_same_thread": False},
    }
    WTF_CSRF_ENABLED = False


@pytest.fixture()
def demo_app():
    return create_app(DemoTestConfig)


def test_seed_refuses_to_run_outside_demo_mode(db, app):
    with pytest.raises(RuntimeError, match="демо-режиме"):
        seed_demo(app)
    assert Nomenclature.query.count() == 0


def test_seed_populates_demo_database(demo_app):
    with demo_app.app_context():
        assert seed_demo(demo_app) is True

        assert Nomenclature.query.count() > 10
        assert ShipmentPlan.query.count() == 2
        assert ShipmentPlanCityDeadline.query.count() > 0
        assert ProductionRecord.query.count() > 0

        statuses = {doc.status for doc in MovementDocument.query.all()}
        assert {"draft", "completed"} <= statuses
        # Есть и принятые, и еще едущие, и принятые с недовозом перемещения.
        docs = MovementDocument.query.filter_by(status="completed").all()
        assert any(doc.received_at is None for doc in docs)
        assert any(doc.received_at is not None for doc in docs)
        assert any(len(list(doc.discrepancies)) for doc in docs)


def test_seed_is_idempotent(demo_app):
    with demo_app.app_context():
        assert seed_demo(demo_app) is True
        count = Nomenclature.query.count()
        assert seed_demo(demo_app) is False
        assert Nomenclature.query.count() == count


def test_demo_users_log_in_with_demo_password_and_banner_is_shown(demo_app):
    with demo_app.app_context():
        seed_demo(demo_app)
        assert User.query.filter_by(username="kladovshik").first().check_password(DEMO_PASSWORD)

    client = demo_app.test_client()
    resp = client.post("/login", data={"username": "admin", "password": DEMO_PASSWORD}, follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "ДЕМО-ВЕРСИЯ" in html

    for path in ("/shipment-plan/", "/nomenclature/", "/movement/", "/receiving/", "/production/efficiency"):
        page = client.get(path)
        assert page.status_code == 200, path


def test_banner_hidden_outside_demo_mode(client_logged_in):
    html = client_logged_in.get("/").get_data(as_text=True)
    assert "ДЕМО-ВЕРСИЯ" not in html
