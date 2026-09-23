"""Заказы на производство (см. чат — панель руководителя, уточненная
схема): этапы до прихода на склад, синхронизация из Google-таблицы
менеджера по кнопке Apps Script (по аналогии с планом отгрузок — см.
test_google_sheets_sync.py). Реальный вызов Google Sheets API не
тестируется — вместо этого read_sheet_tables/resolve_sheet_title/
list_sheet_titles подменяются моком, как и в тестах плана отгрузок."""

from datetime import date, datetime, timedelta

import pytest

from wms.extensions import db
from wms.models import AppSetting, ProductionOrder, User
from wms.utils.production_orders_import import classify_status, map_columns


@pytest.fixture(autouse=True)
def _unhide_production_orders(monkeypatch):
    """Раздел временно скрыт в проде (см. чат — HIDDEN_WORK_IN_PROGRESS в
    wms/blueprints/production_orders.py), но сами тесты продолжают
    проверять реальное поведение страниц, независимо от временного
    тумблера."""
    monkeypatch.setattr("wms.blueprints.production_orders.HIDDEN_WORK_IN_PROGRESS", False)


def _login_as_worker(client, db):
    worker = User(username="worker-po", is_admin=False, role="warehouse")
    worker.set_password("password123")
    db.session.add(worker)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(worker.id)
        sess["_fresh"] = True


def test_classify_status_recognizes_each_stage_by_keyword():
    assert classify_status("Идет поиск цеха") == ("stage", "workshop_search")
    assert classify_status("Отшив образца") == ("stage", "sample_sewing")
    assert classify_status("Образец согласован") == ("stage", "sample_approved")
    assert classify_status("Запрос фото образца") == ("stage", "photo_requested")
    assert classify_status("Заведена карточка на МП") == ("stage", "mp_card_created")
    assert classify_status("Данные в 1С занесено") == ("stage", "data_in_1c")
    assert classify_status("Заказ внесен в 1С") == ("stage", "order_in_1c")


def test_classify_status_recognizes_cancelled_and_rework():
    assert classify_status("Образец отменен") == ("cancelled", None)
    assert classify_status("Заказ отменён поставщиком") == ("cancelled", None)
    assert classify_status("Образец на переделке") == ("rework", None)
    assert classify_status("Отправлен на переделку") == ("rework", None)


def test_classify_status_returns_none_for_unrecognized_text():
    assert classify_status("Что-то непонятное") == (None, None)
    assert classify_status("") == (None, None)
    assert classify_status(None) == (None, None)


def test_map_columns_finds_headers_by_candidate_names():
    headers = ["№ заказа", "Маркетплейс", "Статус", "Дедлайн", "Комментарий"]
    columns = map_columns(headers)
    assert columns["order_number"] == "№ заказа"
    assert columns["marketplace"] == "Маркетплейс"
    assert columns["status"] == "Статус"
    assert columns["deadline"] == "Дедлайн"
    assert all(v is None for v in columns["stage_dates"].values())


def test_map_columns_returns_none_for_missing_columns():
    columns = map_columns(["Что-то", "Другое"])
    assert columns["order_number"] is None
    assert columns["marketplace"] is None
    assert columns["status"] is None
    assert columns["deadline"] is None


def _configure_sheet(monkeypatch, headers, rows, sheet_id="SHEET1", gid="123", sheet_title="Заказы"):
    for key, value in (("production_sheet_id", sheet_id), ("production_sheet_gid", gid)):
        setting = AppSetting.query.get(key)
        if setting is None:
            db.session.add(AppSetting(key=key, value=value))
        else:
            setting.value = value
    db.session.commit()
    monkeypatch.setattr("wms.blueprints.production_orders.google_sheets_configured", lambda app: True)
    monkeypatch.setattr(
        "wms.blueprints.production_orders.resolve_sheet_title", lambda app, sid, gid_: sheet_title
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
            t: (["№ заказа", "Статус"], [{"№ заказа": "ЗК-999", "Статус": "Поиск цеха"}]) for t in titles
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
            [{"№ заказа": "ЗК-102", "Статус": "Заказ внесен в 1С"}],
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
    assert ProductionOrder.query.filter_by(order_number="ЗК-102").first().current_stage == "order_in_1c"
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
    assert ProductionOrder.query.filter_by(order_number="ЗК-200").first().current_stage == "sample_approved"


def test_sync_creates_order_and_stamps_current_stage(db, monkeypatch):
    from wms.blueprints.production_orders import sync_production_orders

    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Маркетплейс", "Статус"],
        rows=[{"№ заказа": "ЗК-001", "Маркетплейс": "Wildberries", "Статус": "Отшив образца"}],
        sheet_title="Заказы",  # без даты в названии — анкер не сработает
    )

    created, updated, diagnostics = sync_production_orders()

    assert created == 1
    assert updated == 0
    order = ProductionOrder.query.filter_by(order_number="ЗК-001").first()
    assert order is not None
    assert order.marketplace == "Wildberries"
    assert order.current_stage == "sample_sewing"
    assert order.workshop_search_started_at is not None
    assert order.sample_sewing_started_at is not None
    assert order.sample_approved_at is None
    assert "Заказы" in diagnostics


def test_sync_uses_sheet_name_date_as_workshop_search_anchor(db, monkeypatch):
    """См. чат: "из таблицы берем дату из названия листа — это точка
    отсчета для поиска производства" — тот же прием, что уже есть для
    листов плана отгрузок (extract_period_start)."""
    from wms.blueprints.production_orders import sync_production_orders

    today = date.today()
    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Статус"],
        rows=[{"№ заказа": "ЗК-ANCHOR", "Статус": "Поиск цеха"}],
        sheet_title=f"Заказы от {today.day:02d}.{today.month:02d}",
    )

    sync_production_orders()

    order = ProductionOrder.query.filter_by(order_number="ЗК-ANCHOR").first()
    assert order.workshop_search_started_at is not None
    assert order.workshop_search_started_at.date() == today


def test_sync_backfills_earlier_stages_with_same_timestamp_when_order_starts_later(db, monkeypatch):
    """Заказ впервые увиден WMS уже на этапе "Образец согласован" — более
    ранние этапы задним числом не восстановить (лист без даты в названии),
    поэтому ставится тот же момент, что и у текущего (честное приближение,
    см. чат)."""
    from wms.blueprints.production_orders import sync_production_orders

    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Статус"],
        rows=[{"№ заказа": "ЗК-002", "Статус": "Образец согласован"}],
        sheet_title="Заказы",
    )

    sync_production_orders()

    order = ProductionOrder.query.filter_by(order_number="ЗК-002").first()
    assert order.current_stage == "sample_approved"
    assert order.workshop_search_started_at == order.sample_approved_at
    assert order.sample_sewing_started_at == order.sample_approved_at
    assert order.photo_requested_at is None


def test_sync_does_not_overwrite_already_stamped_stage_on_repeat_sync(db, monkeypatch):
    """Повторная синхронизация с тем же статусом не должна сдвигать уже
    проставленную дату входа в этап вперед — иначе длительность этапа
    искусственно занижалась бы с каждой новой синхронизацией."""
    from wms.blueprints.production_orders import sync_production_orders

    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Статус"],
        rows=[{"№ заказа": "ЗК-003", "Статус": "Заказ внесен в 1С"}],
    )
    sync_production_orders()
    order = ProductionOrder.query.filter_by(order_number="ЗК-003").first()
    first_stamp = order.order_in_1c_at
    assert first_stamp is not None

    order.order_in_1c_at = datetime.utcnow() - timedelta(days=5)
    db.session.commit()
    backdated = order.order_in_1c_at

    sync_production_orders()
    order = ProductionOrder.query.filter_by(order_number="ЗК-003").first()
    assert order.order_in_1c_at == backdated


def test_sync_prefers_explicit_stage_date_column_over_sync_timestamp(db, monkeypatch):
    from wms.blueprints.production_orders import sync_production_orders

    explicit_date = "01.03.2026"
    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Статус", "Дата поиска цеха"],
        rows=[{"№ заказа": "ЗК-004", "Статус": "Поиск цеха", "Дата поиска цеха": explicit_date}],
        sheet_title="Заказы",
    )

    sync_production_orders()

    order = ProductionOrder.query.filter_by(order_number="ЗК-004").first()
    assert order.workshop_search_started_at == datetime(2026, 3, 1)


def test_sync_handles_cancelled_status(db, monkeypatch):
    from wms.blueprints.production_orders import sync_production_orders

    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Статус"],
        rows=[{"№ заказа": "ЗК-CANCEL", "Статус": "Образец отменен"}],
    )

    _created, _updated, diagnostics = sync_production_orders()

    order = ProductionOrder.query.filter_by(order_number="ЗК-CANCEL").first()
    assert order.current_stage == "sample_cancelled"
    assert order.sample_cancelled_at is not None
    assert "отменено образцов 1" in diagnostics


def test_sync_handles_rework_status_without_advancing_stage(db, monkeypatch):
    from wms.blueprints.production_orders import sync_production_orders

    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Статус"],
        rows=[{"№ заказа": "ЗК-REWORK", "Статус": "Образец на переделке"}],
    )

    _created, _updated, diagnostics = sync_production_orders()

    order = ProductionOrder.query.filter_by(order_number="ЗК-REWORK").first()
    assert order.current_stage == "sample_sewing"
    assert order.rework_count == 1
    assert order.last_rework_at is not None
    assert "отправлено на переделку 1" in diagnostics


def test_sync_rework_after_approval_returns_stage_but_keeps_approval_history(db, monkeypatch):
    order = ProductionOrder(
        order_number="ЗК-REWORK-2",
        current_stage="sample_approved",
        sample_sewing_started_at=datetime.utcnow() - timedelta(days=3),
        sample_approved_at=datetime.utcnow() - timedelta(days=1),
    )
    db.session.add(order)
    db.session.commit()
    first_approval = order.sample_approved_at

    from wms.blueprints.production_orders import sync_production_orders

    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Статус"],
        rows=[{"№ заказа": "ЗК-REWORK-2", "Статус": "Отправлен на переделку"}],
    )
    sync_production_orders()

    order = ProductionOrder.query.filter_by(order_number="ЗК-REWORK-2").first()
    assert order.current_stage == "sample_sewing"
    assert order.rework_count == 1
    # Дата первого согласования не стирается переделкой.
    assert order.sample_approved_at == first_approval


def test_sync_captures_and_updates_deadline_date(db, monkeypatch):
    from wms.blueprints.production_orders import sync_production_orders

    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Статус", "Дедлайн"],
        rows=[{"№ заказа": "ЗК-DEADLINE", "Статус": "Поиск цеха", "Дедлайн": "15.10.2026"}],
    )
    sync_production_orders()
    order = ProductionOrder.query.filter_by(order_number="ЗК-DEADLINE").first()
    assert order.deadline_date == date(2026, 10, 15)

    # Дедлайн может сдвинуться менеджером — берем актуальное значение
    # каждый раз, а не только при первом появлении (в отличие от дат этапов).
    _configure_sheet(
        monkeypatch,
        headers=["№ заказа", "Статус", "Дедлайн"],
        rows=[{"№ заказа": "ЗК-DEADLINE", "Статус": "Поиск цеха", "Дедлайн": "20.10.2026"}],
    )
    sync_production_orders()
    order = ProductionOrder.query.filter_by(order_number="ЗК-DEADLINE").first()
    assert order.deadline_date == date(2026, 10, 20)


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
        current_stage="sample_approved",
        raw_status="Образец согласован",
        sample_approved_at=datetime.utcnow() - timedelta(days=4),
    )
    db.session.add(order)
    db.session.commit()

    html = client_logged_in.get("/production-orders/").get_data(as_text=True)

    assert "ЗК-777" in html
    assert "Образец согласован" in html
    assert ">4<" in html


def test_list_orders_shows_cancelled_order_distinctly(db, client_logged_in):
    order = ProductionOrder(
        order_number="ЗК-888",
        current_stage="sample_cancelled",
        raw_status="Образец отменен",
        sample_cancelled_at=datetime.utcnow() - timedelta(days=2),
    )
    db.session.add(order)
    db.session.commit()

    html = client_logged_in.get("/production-orders/").get_data(as_text=True)

    assert "ЗК-888" in html
    assert "Образец отменен" in html
    assert ">2<" in html


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


def test_hidden_while_in_progress_redirects_away_but_google_trigger_still_works(
    client_logged_in, client, db, monkeypatch
):
    """Флаг из чата ("пока скрой... будем доделывать") — по умолчанию True в
    самом модуле; проверяем это отдельно от остальных тестов файла (которые
    его отключают). google_trigger нарочно не блокируется — синхронизация
    из уже настроенной кнопки в таблице должна продолжать тихо работать."""
    monkeypatch.setattr("wms.blueprints.production_orders.HIDDEN_WORK_IN_PROGRESS", True)

    assert client_logged_in.get("/production-orders/").status_code == 302
    assert client_logged_in.get("/production-orders/settings").status_code == 302

    db.session.add(AppSetting(key="production_sheet_token", value="secret-token"))
    db.session.commit()
    monkeypatch.setattr(
        "wms.blueprints.production_orders.sync_production_orders", lambda: (0, 0, "диагностика")
    )
    response = client.post(
        "/production-orders/google-trigger", headers={"X-WMS-Sync-Token": "secret-token"}
    )
    assert response.status_code == 200
