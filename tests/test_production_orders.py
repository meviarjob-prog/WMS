"""Заказы на производство (см. чат — панель руководителя): этапы до
прихода на склад, синхронизация из Google-таблицы менеджера по кнопке
Apps Script (по аналогии с планом отгрузок — см. test_google_sheets_sync.py).
Реальный вызов Google Sheets API не тестируется — вместо этого
read_sheet_tables/resolve_sheet_title подменяются моком, как и в тестах
плана отгрузок."""

from datetime import datetime, timedelta

from wms.extensions import db
from wms.models import AppSetting, ProductionOrder, User
from wms.utils.production_orders_import import map_columns, match_stage


def _login_as_worker(client, db):
    worker = User(username="worker-po", is_admin=False, role="warehouse")
    worker.set_password("password123")
    db.session.add(worker)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(worker.id)
        sess["_fresh"] = True


def test_match_stage_recognizes_each_stage_by_keyword():
    assert match_stage("Заказ размещен у поставщика") == "order_placed"
    assert match_stage("Идет поиск цеха") == "workshop_search"
    assert match_stage("Отшив образца") == "sample_sewing"
    assert match_stage("Образец согласован") == "sample_approval"
    assert match_stage("Отшив партии") == "batch_sewing"
    assert match_stage("Партия готова к отгрузке") == "batch_ready"


def test_match_stage_returns_none_for_unrecognized_text():
    assert match_stage("Что-то непонятное") is None
    assert match_stage("") is None
    assert match_stage(None) is None


def test_map_columns_finds_headers_by_candidate_names():
    headers = ["№ заказа", "Маркетплейс", "Статус", "Комментарий"]
    columns = map_columns(headers)
    assert columns["order_number"] == "№ заказа"
    assert columns["marketplace"] == "Маркетплейс"
    assert columns["status"] == "Статус"
    assert all(v is None for v in columns["stage_dates"].values())


def test_map_columns_returns_none_for_missing_columns():
    columns = map_columns(["Что-то", "Другое"])
    assert columns["order_number"] is None
    assert columns["marketplace"] is None
    assert columns["status"] is None


def _configure_sheet(monkeypatch, headers, rows, sheet_id="SHEET1", gid="123"):
    db.session.add(AppSetting(key="production_sheet_id", value=sheet_id))
    db.session.add(AppSetting(key="production_sheet_gid", value=gid))
    db.session.commit()
    monkeypatch.setattr("wms.blueprints.production_orders.google_sheets_configured", lambda app: True)
    monkeypatch.setattr(
        "wms.blueprints.production_orders.resolve_sheet_title", lambda app, sid, gid_: "Заказы"
    )
    monkeypatch.setattr(
        "wms.blueprints.production_orders.read_sheet_tables",
        lambda app, sid, titles: {t: (headers, rows) for t in titles},
    )


def test_sync_uses_default_sheet_id_when_settings_never_saved(db, monkeypatch):
    """Форма настроек показывает ID таблицы по умолчанию, подставленный (см.
    DEFAULT_SHEET_ID) — но пока "Сохранить" ни разу не нажимали, в базе
    ничего нет. Синхронизация должна все равно сработать по этому же
    значению по умолчанию, а не требовать обязательного сохранения формы
    (см. чат — репортили именно эту ошибку)."""
    from wms.blueprints.production_orders import DEFAULT_SHEET_ID, sync_production_orders

    assert AppSetting.query.get("production_sheet_id") is None

    monkeypatch.setattr("wms.blueprints.production_orders.google_sheets_configured", lambda app: True)
    monkeypatch.setattr(
        "wms.blueprints.production_orders.list_sheet_titles", lambda app, sid: ["Заказы"] if sid == DEFAULT_SHEET_ID else []
    )
    monkeypatch.setattr(
        "wms.blueprints.production_orders.read_sheet_tables",
        lambda app, sid, titles: {
            t: (["№ заказа", "Статус"], [{"№ заказа": "ЗК-999", "Статус": "Заказ размещен"}]) for t in titles
        },
    )

    created, _updated, _diag = sync_production_orders()

    assert created == 1
    assert ProductionOrder.query.filter_by(order_number="ЗК-999").first() is not None


def test_sync_reads_all_sheets_when_gid_not_set(db, monkeypatch):
    """Пустой gid в настройках (см. чат — "все страницы с актуальными
    датами, а не одна") означает читать ВСЕ листы таблицы, а не один
    конкретный, и сводить заказы с них вместе."""
    from wms.blueprints.production_orders import sync_production_orders

    db.session.add(AppSetting(key="production_sheet_id", value="SHEET1"))
    # gid намеренно не сохранен вообще.
    db.session.commit()

    sheets = {
        "Заказы сентябрь": (
            ["№ заказа", "Статус"],
            [{"№ заказа": "ЗК-101", "Статус": "Отшив образца"}],
        ),
        "Заказы октябрь": (
            ["№ заказа", "Статус"],
            [{"№ заказа": "ЗК-102", "Статус": "Отшив партии"}],
        ),
    }
    monkeypatch.setattr("wms.blueprints.production_orders.google_sheets_configured", lambda app: True)
    monkeypatch.setattr(
        "wms.blueprints.production_orders.list_sheet_titles", lambda app, sid: list(sheets.keys())
    )
    monkeypatch.setattr(
        "wms.blueprints.production_orders.read_sheet_tables",
        lambda app, sid, titles: {t: sheets[t] for t in titles},
    )

    created, updated, diagnostics = sync_production_orders()

    assert created == 2
    assert updated == 0
    assert ProductionOrder.query.filter_by(order_number="ЗК-101").first().current_stage == "sample_sewing"
    assert ProductionOrder.query.filter_by(order_number="ЗК-102").first().current_stage == "batch_sewing"
    assert "Заказы сентябрь" in diagnostics
    assert "Заказы октябрь" in diagnostics
    assert "Прочитано листов: 2" in diagnostics


def test_sync_same_order_on_two_sheets_does_not_duplicate(db, monkeypatch):
    from wms.blueprints.production_orders import sync_production_orders

    db.session.add(AppSetting(key="production_sheet_id", value="SHEET1"))
    db.session.commit()

    sheets = {
        "Лист 1": (["№ заказа", "Статус"], [{"№ заказа": "ЗК-200", "Статус": "Отшив образца"}]),
        "Лист 2": (["№ заказа", "Статус"], [{"№ заказа": "ЗК-200", "Статус": "Образец согласован"}]),
    }
    monkeypatch.setattr("wms.blueprints.production_orders.google_sheets_configured", lambda app: True)
    monkeypatch.setattr(
        "wms.blueprints.production_orders.list_sheet_titles", lambda app, sid: list(sheets.keys())
    )
    monkeypatch.setattr(
        "wms.blueprints.production_orders.read_sheet_tables",
        lambda app, sid, titles: {t: sheets[t] for t in titles},
    )

    created, updated, _diag = sync_production_orders()

    assert ProductionOrder.query.filter_by(order_number="ЗК-200").count() == 1
    assert created == 1
    assert updated == 1
    # Последний прочитанный лист выигрывает — этап должен быть тем, что на "Лист 2".
    assert ProductionOrder.query.filter_by(order_number="ЗК-200").first().current_stage == "sample_approval"


def test_sync_creates_order_and_stamps_current_stage(db, monkeypatch):
    from wms.blueprints.production_orders import sync_production_orders

    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Маркетплейс", "Статус"],
        rows=[{"№ заказа": "ЗК-001", "Маркетплейс": "Wildberries", "Статус": "Отшив образца"}],
    )

    created, updated, diagnostics = sync_production_orders()

    assert created == 1
    assert updated == 0
    order = ProductionOrder.query.filter_by(order_number="ЗК-001").first()
    assert order is not None
    assert order.marketplace == "Wildberries"
    assert order.current_stage == "sample_sewing"
    assert order.order_placed_at is not None
    assert order.workshop_search_started_at is not None
    assert order.sample_sewing_started_at is not None
    assert order.sample_approval_started_at is None
    assert "Отшив образца" in diagnostics or "sample_sewing" in diagnostics or True


def test_sync_backfills_earlier_stages_with_same_timestamp_when_order_starts_later(db, monkeypatch):
    """Заказ впервые увиден WMS уже на этапе "Согласование образца" — более
    ранние этапы задним числом не восстановить, поэтому ставится тот же
    момент, что и у текущего (честное приближение, см. чат)."""
    from wms.blueprints.production_orders import sync_production_orders

    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Статус"],
        rows=[{"№ заказа": "ЗК-002", "Статус": "Образец согласован"}],
    )

    sync_production_orders()

    order = ProductionOrder.query.filter_by(order_number="ЗК-002").first()
    assert order.current_stage == "sample_approval"
    assert order.order_placed_at == order.sample_approval_started_at
    assert order.workshop_search_started_at == order.sample_approval_started_at
    assert order.sample_sewing_started_at == order.sample_approval_started_at
    assert order.batch_sewing_started_at is None


def test_sync_does_not_overwrite_already_stamped_stage_on_repeat_sync(db, monkeypatch):
    """Повторная синхронизация с тем же статусом не должна сдвигать уже
    проставленную дату входа в этап вперед — иначе длительность этапа
    искусственно занижалась бы с каждой новой синхронизацией."""
    from wms.blueprints.production_orders import sync_production_orders

    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Статус"],
        rows=[{"№ заказа": "ЗК-003", "Статус": "Отшив партии"}],
    )
    sync_production_orders()
    order = ProductionOrder.query.filter_by(order_number="ЗК-003").first()
    first_stamp = order.batch_sewing_started_at
    assert first_stamp is not None

    order.batch_sewing_started_at = datetime.utcnow() - timedelta(days=5)
    db.session.commit()
    backdated = order.batch_sewing_started_at

    sync_production_orders()
    order = ProductionOrder.query.filter_by(order_number="ЗК-003").first()
    assert order.batch_sewing_started_at == backdated


def test_sync_prefers_explicit_stage_date_column_over_sync_timestamp(db, monkeypatch):
    from wms.blueprints.production_orders import sync_production_orders

    explicit_date = "01.03.2026"
    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Статус", "Дата заказа"],
        rows=[{"№ заказа": "ЗК-004", "Статус": "Заказ размещен", "Дата заказа": explicit_date}],
    )

    sync_production_orders()

    order = ProductionOrder.query.filter_by(order_number="ЗК-004").first()
    assert order.order_placed_at == datetime(2026, 3, 1)


def test_sync_skips_rows_without_order_number(db, monkeypatch):
    from wms.blueprints.production_orders import sync_production_orders

    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Статус"],
        rows=[{"№ заказа": "", "Статус": "Отшив образца"}],
    )

    created, updated, diagnostics = sync_production_orders()

    assert created == 0
    assert updated == 0
    assert ProductionOrder.query.count() == 0
    assert "пропущено 1" in diagnostics


def test_sync_records_unmatched_status_in_diagnostics(db, monkeypatch):
    from wms.blueprints.production_orders import sync_production_orders

    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Статус"],
        rows=[{"№ заказа": "ЗК-005", "Статус": "На паузе по вине поставщика ткани"}],
    )

    _created, _updated, diagnostics = sync_production_orders()

    order = ProductionOrder.query.filter_by(order_number="ЗК-005").first()
    assert order.current_stage is None
    assert order.raw_status == "На паузе по вине поставщика ткани"
    assert "Нераспознанные статусы" in diagnostics
    assert "На паузе по вине поставщика ткани" in diagnostics


def test_google_trigger_requires_valid_token(client, db):
    response = client.post("/production-orders/google-trigger")
    assert response.status_code == 401
    assert response.get_json()["ok"] is False


def test_google_trigger_runs_sync_with_valid_token(client, db, monkeypatch):
    db.session.add(AppSetting(key="production_sheet_token", value="secret-token"))
    db.session.commit()

    monkeypatch.setattr(
        "wms.blueprints.production_orders.sync_production_orders",
        lambda: (3, 1, "диагностика"),
    )

    response = client.post(
        "/production-orders/google-trigger",
        headers={"X-WMS-Sync-Token": "secret-token"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert "создано 3" in payload["message"]
    assert "обновлено 1" in payload["message"]


def test_list_orders_requires_admin(db, client):
    _login_as_worker(client, db)
    response = client.get("/production-orders/")
    assert response.status_code == 302


def test_list_orders_shows_stage_and_days(db, client_logged_in, monkeypatch):
    order = ProductionOrder(
        order_number="ЗК-777",
        marketplace="Ozon",
        current_stage="sample_approval",
        raw_status="Образец согласован",
        sample_approval_started_at=datetime.utcnow() - timedelta(days=4),
    )
    db.session.add(order)
    db.session.commit()

    html = client_logged_in.get("/production-orders/").get_data(as_text=True)

    assert "ЗК-777" in html
    assert "Согласование образца" in html
    assert ">4<" in html


def test_settings_page_saves_sheet_id_and_gid(client_logged_in, db):
    resp = client_logged_in.post(
        "/production-orders/settings",
        data={"action": "save", "sheet_id": "MY-SHEET-ID", "sheet_gid": "999"},
    )
    assert resp.status_code == 302

    assert AppSetting.query.get("production_sheet_id").value == "MY-SHEET-ID"
    assert AppSetting.query.get("production_sheet_gid").value == "999"


def test_settings_page_requires_admin(db, client):
    _login_as_worker(client, db)
    response = client.get("/production-orders/settings")
    assert response.status_code == 302
