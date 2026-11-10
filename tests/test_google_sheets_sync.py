from datetime import date, datetime, time, timedelta
import io
import json

from openpyxl import Workbook

from wms.extensions import db
from wms.models import (
    AppSetting,
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    MovementReceiptDiscrepancy,
    Nomenclature,
    ShipmentPlan,
    ShipmentPlanLine,
    Warehouse,
)

from wms.utils.google_sheets import (
    _current_plan_fact_totals,
    _ensure_output_row_capacity,
    _fact_ranges_for_sheet,
    build_wms_movement_rows,
    distribution_sheet_titles,
    movement_wms_totals,
)


class _ExecuteRecorder:
    def __init__(self):
        self.calls = []

    def spreadsheets(self):
        return self

    def batchUpdate(self, **kwargs):
        self.calls.append(kwargs)
        return self

    def execute(self):
        return {}


def test_distribution_sheet_titles_ignores_dates_and_non_distribution_sheets():
    titles = distribution_sheet_titles(
        [
            "Распределение FBO ВБ от 12.09",
            "Распределение ОЗОН ФБС от 27.08",
            " ВБ ФБС от 27.08",
            "Приоритет",
        ]
    )
    assert titles == [
        "Распределение FBO ВБ от 12.09",
        "Распределение ОЗОН ФБС от 27.08",
    ]


def test_wms_rows_separate_in_transit_and_received(db):
    sender = Warehouse(code="SYNC-FROM", name="Основной")
    target = Warehouse(
        code="SYNC-TO", name="ОЗОН: Москва", marketplace="ozon", marketplace_city="Москва"
    )
    item = Nomenclature(sku="ART-1", barcode="4600000000001", name="Товар", unit="шт")
    db.session.add_all([sender, target, item])
    db.session.commit()

    for number, received in (("MOVE-TRANSIT", False), ("MOVE-RECEIVED", True)):
        box = Box(box_number=f"BOX-{number}", warehouse_id=target.id, status="open")
        document = MovementDocument(
            number=number,
            from_warehouse_id=sender.id,
            to_warehouse_id=target.id,
            status="completed",
            shipped_at=datetime.utcnow(),
            received_at=datetime.utcnow() if received else None,
            marketplace_request_created_at=datetime.utcnow(),
        )
        db.session.add_all([box, document])
        db.session.flush()
        db.session.add_all(
            [
                BoxItem(box_id=box.id, nomenclature_id=item.id, qty=5),
                MovementLine(document_id=document.id, box_id=box.id, from_warehouse_id=sender.id),
            ]
        )
    db.session.commit()

    rows = build_wms_movement_rows()
    assert len(rows) == 1
    assert rows[0][6:] == [5.0, 5.0, 10.0]

    plan = ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    db.session.flush()
    db.session.add(
        ShipmentPlanLine(
            plan_id=plan.id,
            warehouse_id=target.id,
            nomenclature_id=item.id,
            barcode=item.barcode,
            planned_qty=20,
            # Даже если сохраненный счетчик строки плана устарел, факт
            # восстанавливается непосредственно из перемещений WMS.
            fulfilled_qty=0,
        )
    )
    db.session.commit()

    assert _current_plan_fact_totals()[("ozon", "москва", item.barcode)] == 10


def test_wms_rows_count_only_explicit_transport_pickup(db):
    """Факт отгрузки начинается с отдельной отметки передачи транспорту."""
    sender = Warehouse(code="SYNC-NUM-FROM", name="Основной")
    target = Warehouse(
        code="SYNC-NUM-TO",
        name="ВБ: Казань",
        marketplace="wb",
        marketplace_city="Казань",
    )
    item = Nomenclature(sku="ART-NUM", barcode="4600000000099", name="Товар", unit="шт")
    box = Box(box_number="BOX-REQUEST-NUMBER", warehouse=sender, status="shipped")
    document = MovementDocument(
        number="MOVE-REQUEST-NUMBER",
        from_warehouse=sender,
        to_warehouse=target,
        status="completed",
        shipped_at=datetime.utcnow(),
    )
    db.session.add_all([sender, target, item, box, document])
    db.session.flush()
    db.session.add_all(
        [
            BoxItem(box_id=box.id, nomenclature_id=item.id, qty=7669),
            MovementLine(document_id=document.id, box_id=box.id, from_warehouse_id=sender.id),
        ]
    )
    db.session.commit()

    rows = build_wms_movement_rows()

    assert len(rows) == 1
    assert rows[0][6:] == [7669.0, 0.0, 7669.0]


def test_period_totals_sum_all_cities_from_0001_and_keep_sent_qty_on_shortage(db):
    sender = Warehouse(code="SYNC-DATE-FROM", name="Основной")
    kazan = Warehouse(
        code="SYNC-DATE-KZN", name="ВБ: Казань", marketplace="wb", marketplace_city="Казань"
    )
    samara = Warehouse(
        code="SYNC-DATE-SMR", name="ВБ: Самара", marketplace="wb", marketplace_city="Самара"
    )
    item = Nomenclature(sku="ART-DATE", barcode="4600000000088", name="Товар", unit="шт")
    db.session.add_all([sender, kazan, samara, item])
    db.session.flush()

    period_start = date(2026, 9, 12)

    def add_movement(number, warehouse, qty, completed_at, received_qty=None):
        box = Box(box_number=f"BOX-{number}", warehouse=sender, status="shipped")
        document = MovementDocument(
            number=number,
            from_warehouse=sender,
            to_warehouse=warehouse,
            status="completed",
            completed_at=completed_at,
            shipped_at=completed_at,
            received_at=(completed_at + timedelta(days=1)) if received_qty is not None else None,
        )
        db.session.add_all([box, document])
        db.session.flush()
        db.session.add_all(
            [
                BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty),
                MovementLine(document_id=document.id, box_id=box.id, from_warehouse_id=sender.id),
            ]
        )
        if received_qty is not None:
            db.session.add(
                MovementReceiptDiscrepancy(
                    document_id=document.id,
                    nomenclature_id=item.id,
                    expected_qty=qty,
                    received_qty=received_qty,
                )
            )

    add_movement("MOVE-BEFORE", kazan, 100, datetime.combine(period_start, time(0, 0)))
    add_movement("MOVE-KZN-1", kazan, 20, datetime.combine(period_start, time(0, 1)))
    add_movement(
        "MOVE-KZN-2",
        kazan,
        30,
        datetime.combine(period_start, time(10, 0)),
        received_qty=25,
    )
    add_movement("MOVE-SMR", samara, 7, datetime.combine(period_start, time(12, 0)))
    db.session.commit()

    totals = movement_wms_totals(period_start)

    assert totals[(kazan.id, item.id)]["shipped"] == 50
    assert totals[(kazan.id, item.id)]["received"] == 25
    assert totals[(samara.id, item.id)]["shipped"] == 7


def test_fact_ranges_target_only_shipment_fact_column():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Распределение ВБ"
    sheet.append(["Артикул", "Баркод", "Москва", "отгружено"])
    sheet.append(["A1", "111", 10, 0])
    sheet.append(["ИТОГО", None, 10, "=SUM(D2:D2)"])

    ranges = _fact_ranges_for_sheet(sheet, "wb", {("wb", "москва", "111"): 7})

    assert ranges == [{"range": "'Распределение ВБ'!D2:D3", "values": [[7], [None]]}]


def test_fact_ranges_match_city_aliases_when_writing_back_to_google():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Распределение ВБ"
    sheet.append(["Артикул", "Баркод", "СПБ", "отгружено"])
    sheet.append(["A1", "111", 10, 0])

    ranges = _fact_ranges_for_sheet(
        sheet,
        "wb",
        {("wb", "санкт-петербург", "111"): 9},
    )

    assert ranges == [{"range": "'Распределение ВБ'!D2:D2", "values": [[9]]}]


def test_output_sheet_expands_only_when_required_rows_exceed_grid():
    service = _ExecuteRecorder()
    properties = {"sheetId": 42, "gridProperties": {"rowCount": 2139}}

    assert _ensure_output_row_capacity(service, "sheet-id", properties, 2139) == 2139
    assert service.calls == []

    assert _ensure_output_row_capacity(service, "sheet-id", properties, 2140) == 2140
    assert service.calls[0]["body"]["requests"][0]["updateSheetProperties"][
        "properties"
    ]["gridProperties"]["rowCount"] == 2140


def test_google_trigger_is_public_but_requires_its_own_token(client):
    response = client.post("/shipment-plan/google-trigger")

    assert response.status_code == 401
    assert response.get_json()["ok"] is False


def test_google_trigger_runs_sync_with_valid_token(client, db, monkeypatch):
    db.session.add(AppSetting(key="google_sheets_trigger_token", value="secret-token"))
    db.session.commit()

    monkeypatch.setattr(
        "wms.blueprints.shipment_plan.google_sheets_configured", lambda app: True
    )
    monkeypatch.setattr(
        "wms.blueprints.shipment_plan.sync_google_plans_and_movements",
        lambda: (["ВБ: 12 позиций"], ["Распределение ВБ"], 4, 8),
    )

    response = client.post(
        "/shipment-plan/google-trigger",
        headers={"X-WMS-Sync-Token": "secret-token"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert "ВБ: 12 позиций" in payload["message"]
    assert "обновлено ячеек факта: 8" in payload["message"]


def test_sync_reports_missing_sheet_per_marketplace_and_leaves_old_plan(db, monkeypatch, app):
    """Раньше, если для площадки не находился подходящий лист «Распределение»
    (например, его переименовали/удалили), она молча пропускалась — план
    оставался старым, а в сообщении о синхронизации по ней не было вообще
    ни строки, и "успешная" синхронизация выглядела так, будто обновилось
    всё (см. чат: "ошибка не ушла и новые позиции не подтягивает"). Теперь
    для площадки без листа явно сообщается "лист не найден", а её план
    остается нетронутым, не стирается в пустоту."""
    from wms.blueprints import shipment_plan as sp

    wh = Warehouse(code="WH-SYNC-MISS", name="ОЗОН: Москва", marketplace="ozon", marketplace_city="Москва")
    db.session.add(wh)
    db.session.commit()
    old_plan = ShipmentPlan(marketplace="ozon", sheet_name="Распределение ОЗОН ФБО от 01.09 (копия)")
    db.session.add(old_plan)
    db.session.commit()
    old_line = ShipmentPlanLine(
        plan_id=old_plan.id, warehouse_id=wh.id, barcode="OLDBARCODE", planned_qty=10,
    )
    db.session.add(old_line)
    db.session.commit()

    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("Распределение ВБ ФБС от 05.09")
    ws.append(["Артикул", "Размер", "Баркод", "Москва"])
    ws.append(["A1", "46", "1112223334445", 5])
    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)

    monkeypatch.setattr(
        sp, "load_distribution_workbook", lambda app: (stream, ["Распределение ВБ ФБС от 05.09"])
    )
    monkeypatch.setattr(sp, "write_wms_movement_sheet", lambda app: 0)
    monkeypatch.setattr(sp, "write_distribution_facts", lambda app, workbook: 0)

    with app.test_request_context():
        summary, sheet_names, exported, updated_cells = sp.sync_google_plans_and_movements()

    ozon_summary = next(line for line in summary if line.startswith("ОЗОН"))
    assert "лист" in ozon_summary and "не найден" in ozon_summary
    assert "НЕ обновлен" in ozon_summary

    # Старый план площадки без найденного листа остался как был.
    assert ShipmentPlan.query.get(old_plan.id) is not None
    assert ShipmentPlanLine.query.filter_by(plan_id=old_plan.id).count() == 1
    assert ShipmentPlanLine.query.get(old_line.id).barcode == "OLDBARCODE"

    # Персистентная отметка — видна на дашборде при каждом заходе, не
    # только в одноразовом flash-сообщении этого конкретного запроса.
    assert sp._google_sync_status()["missing_marketplaces"] == "ОЗОН"


def test_google_button_setup_uses_public_https_address(client_logged_in, app):
    app.config["WMS_PUBLIC_URL"] = "https://wms.wmsmeviar.ru"

    response = client_logged_in.get("/shipment-plan/google-button")

    assert response.status_code == 200
    assert b"https://wms.wmsmeviar.ru/shipment-plan/google-trigger" in response.data
    assert b"syncWms" in response.data


def test_dashboard_shows_google_button_setup_even_without_credentials(
    client_logged_in, monkeypatch
):
    monkeypatch.setattr(
        "wms.blueprints.shipment_plan.google_sheets_configured", lambda app: False
    )

    response = client_logged_in.get("/shipment-plan/")

    assert response.status_code == 200
    assert "/shipment-plan/google-button" in response.get_data(as_text=True)


def test_admin_can_upload_google_credentials_from_setup_page(
    client_logged_in, app, tmp_path
):
    target = tmp_path / "google-service-account.json"
    app.config["GOOGLE_SERVICE_ACCOUNT_FILE"] = str(target)
    credentials = {
        "type": "service_account",
        "project_id": "test-project",
        "private_key": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
        "client_email": "wms@example.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }

    response = client_logged_in.post(
        "/shipment-plan/google-button",
        data={
            "action": "upload_credentials",
            "credentials": (
                io.BytesIO(json.dumps(credentials).encode("utf-8")),
                "credentials.json",
            ),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["project_id"] == "test-project"
    assert "Ключ Google сохранен на сервере" in response.get_data(as_text=True)


def _background_env(monkeypatch, app, sync_impl):
    """Включает фоновый режим (в тестах он выключен флагом TESTING) и
    запускает «поток» тут же, в этом же потоке выполнения."""
    from wms.blueprints import shipment_plan as sp

    app.config["SYNC_GOOGLE_INLINE"] = False

    class InlineThread:
        def __init__(self, target, args=(), daemon=None):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(sp.threading, "Thread", InlineThread)
    monkeypatch.setattr(sp, "sync_google_plans_and_movements", sync_impl)
    return sp


def test_sync_button_runs_in_background_and_stores_result(db, app, client_logged_in, monkeypatch):
    sp = _background_env(
        monkeypatch, app,
        lambda uploaded_by_id=None: (["ОЗОН: 3 позиций"], ["Распределение"], 5, 7),
    )
    resp = client_logged_in.post("/shipment-plan/sync-google", follow_redirects=True)
    assert "запущена в фоне" in resp.get_data(as_text=True)

    status = sp._google_sync_status()
    assert status["running"] is False
    assert "Google Таблица синхронизирована" in status["last_result"] and "обновлено ячеек факта: 7" in status["last_result"]
    assert sp._google_sync_lock.acquire(False)      # блокировка освобождена
    sp._google_sync_lock.release()


def test_background_sync_error_is_saved_not_raised(db, app, client_logged_in, monkeypatch):
    def boom(uploaded_by_id=None):
        raise RuntimeError("Google недоступен")

    sp = _background_env(monkeypatch, app, boom)
    client_logged_in.post("/shipment-plan/sync-google")
    status = sp._google_sync_status()
    assert status["running"] is False
    assert "Google недоступен" in status["last_error"] and "Не удалось" in status["last_result"]


def test_second_click_while_running_is_refused_but_stale_marker_is_ignored(db, app, client_logged_in, monkeypatch):
    from datetime import datetime, timedelta

    calls = []
    sp = _background_env(
        monkeypatch, app,
        lambda uploaded_by_id=None: calls.append(1) or (["x"], ["Лист"], 0, 0),
    )
    sp._set_sync_setting(sp.GOOGLE_SYNC_RUNNING_KEY, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    db.session.commit()
    resp = client_logged_in.post("/shipment-plan/sync-google", follow_redirects=True)
    assert "уже выполняется" in resp.get_data(as_text=True) and calls == []

    old = (datetime.utcnow() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    sp._set_sync_setting(sp.GOOGLE_SYNC_RUNNING_KEY, old)
    db.session.commit()
    client_logged_in.post("/shipment-plan/sync-google")
    assert calls == [1]


def test_status_endpoint_is_admin_only_and_reports_running(db, app, client_logged_in, monkeypatch):
    resp = client_logged_in.get("/shipment-plan/sync-google/status")
    assert resp.get_json() == {"running": False, "result": ""}
