from datetime import datetime, timedelta

import pytest

from wms.extensions import db
from wms.models import MovementDocument, ProductionOrder, User, Warehouse


@pytest.fixture(autouse=True)
def _unhide_management_dashboard(monkeypatch):
    """Раздел временно скрыт в проде (см. чат — HIDDEN_WORK_IN_PROGRESS в
    wms/blueprints/management.py), но сами тесты продолжают проверять
    реальное поведение страницы, независимо от временного тумблера."""
    monkeypatch.setattr("wms.blueprints.management.HIDDEN_WORK_IN_PROGRESS", False)


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


def test_dashboard_renders_problem_movement_destination(db, client_logged_in):
    sender = Warehouse(code="MGMT-ALERT-S", name="Основной")
    destination = Warehouse(
        code="MGMT-ALERT-D",
        name="ВБ: Казань",
        marketplace="wb",
        marketplace_city="Казань",
    )
    db.session.add_all([sender, destination])
    db.session.commit()
    document = MovementDocument(
        number="PER-MGMT-ALERT",
        from_warehouse_id=sender.id,
        to_warehouse_id=destination.id,
        status="completed",
        created_at=datetime.utcnow() - timedelta(hours=8),
        completed_at=datetime.utcnow() - timedelta(hours=6),
    )
    db.session.add(document)
    db.session.commit()

    response = client_logged_in.get("/management/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "PER-MGMT-ALERT" in html
    assert "ВБ: Казань" in html


def test_process_flow_shows_production_stage_awaiting_connection_before_sync(client_logged_in):
    """Пока Google-таблица заказов (см. production_orders.py) ни разу не
    синхронизировалась, первые три этапа воронки остаются "ожидает
    подключение" — как и до появления ProductionOrder вообще."""
    html = client_logged_in.get("/management/").get_data(as_text=True)
    assert "ожидает подключение" in html


def test_process_flow_uses_real_production_order_counts_after_sync(db, client_logged_in):
    db.session.add_all(
        [
            ProductionOrder(order_number="ЗК-MGMT-1", current_stage="workshop_search"),
            ProductionOrder(order_number="ЗК-MGMT-2", current_stage="sample_sewing"),
            ProductionOrder(order_number="ЗК-MGMT-3", current_stage="sample_approved"),
            ProductionOrder(order_number="ЗК-MGMT-4", current_stage="photo_requested"),
            ProductionOrder(order_number="ЗК-MGMT-5", current_stage="mp_card_created"),
        ]
    )
    db.session.commit()

    html = client_logged_in.get("/management/").get_data(as_text=True)

    assert "ожидает подключение" not in html
    # "Поиск цеха / отшив образца" — workshop_search + sample_sewing = 2.
    assert '<div class="process-value">2</div>' in html
    # "Согласование / фото / карточка МП" — sample_approved + photo_requested + mp_card_created = 3.
    assert '<div class="process-value">3</div>' in html


def test_hidden_while_in_progress_redirects_away(client_logged_in, monkeypatch):
    """Флаг из чата ("пока скрой панель руководителя... будем доделывать") —
    по умолчанию True в самом модуле; здесь проверяем реальное поведение
    тумблера отдельно от остальных тестов файла (которые его отключают)."""
    monkeypatch.setattr("wms.blueprints.management.HIDDEN_WORK_IN_PROGRESS", True)

    response = client_logged_in.get("/management/")

    assert response.status_code == 302
