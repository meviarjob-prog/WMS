"""Заказы на производство — этапы до прихода на склад (поиск поставщика/
цеха -> отшив образца -> согласование/отмена/переделка -> запрос фото ->
карточка на МП -> данные в 1С -> заказ в 1С -> [дедлайн партии] -> обычный
процесс WMS), см. чат (панель руководителя, уточненная схема). Данные
ведет менеджер в отдельной Google-таблице; WMS синхронизирует их так же,
как план отгрузок (см. shipment_plan.py) — кнопкой из Apps Script,
встроенной прямо в таблицу, никакого отдельного логина/пароля."""

import hmac
import secrets
from datetime import datetime

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import (
    AppSetting,
    PRODUCTION_ORDER_STAGE_CANCELLED,
    PRODUCTION_ORDER_STAGE_KEYS,
    PRODUCTION_ORDER_STAGE_LABELS,
    ProductionOrder,
)
from ..utils.google_sheets import (
    google_sheets_configured,
    list_sheet_titles,
    read_sheet_tables,
    resolve_sheet_title,
)
from ..utils.production_orders_import import classify_status, map_columns
from ..utils.shipment_plan_import import extract_period_start

bp = Blueprint("production_orders", __name__)

SHEET_ID_KEY = "production_sheet_id"
SHEET_GID_KEY = "production_sheet_gid"
# ID подставляется по умолчанию в форму настроек, пока его явно не
# сохранили — это таблица заказов на производство и статусов, которую
# прислали в чате. Нажатие "Сохранить" без изменений зафиксирует его как
# обычную настройку — ничего не подключается само по себе.
# gid по умолчанию НЕ подставляется (пусто = читать все листы таблицы,
# см. чат — "все страницы с актуальными датами, а не одна") — конкретный
# лист указывают только если нужно ограничиться одним.
DEFAULT_SHEET_ID = "1Pakv6rjDxXtObySNkED2KzF9XFgCXUILOA8SmCDUPWA"
DEFAULT_SHEET_GID = ""
SYNC_TOKEN_KEY = "production_sheet_token"
SYNC_AT_KEY = "production_sheet_synced_at"
SYNC_ERROR_KEY = "production_sheet_error"
DIAGNOSTICS_KEY = "production_sheet_diagnostics"

GOOGLE_SHEETS_PUBLIC_ENDPOINTS = {"production_orders.google_trigger"}

# Временно скрыто по просьбе в чате ("пока скрой... будем доделывать") —
# раздел еще дорабатывается. Переключить обратно на False, когда будете
# готовы показать снова (ссылка в base.html скрыта отдельно). google_trigger
# нарочно не блокируется — если синхронизация уже настроена в самой
# таблице, пусть продолжает тихо работать в фоне, не показываясь в UI.
HIDDEN_WORK_IN_PROGRESS = True


@bp.before_request
def _hide_pages_while_in_progress():
    if HIDDEN_WORK_IN_PROGRESS and request.endpoint != "production_orders.google_trigger":
        flash("Заказы на производство временно скрыты — дорабатывается", "warning")
        return redirect(url_for("main.index"))
    return None


def _get_setting(key, default=None):
    setting = AppSetting.query.get(key)
    return setting.value if setting and setting.value else default


def _set_setting(key, value):
    setting = AppSetting.query.get(key)
    if setting is None:
        setting = AppSetting(key=key)
        db.session.add(setting)
    setting.value = (value or "")[:4000]


def _get_or_create_token(rotate=False):
    setting = AppSetting.query.get(SYNC_TOKEN_KEY)
    if setting is None:
        setting = AppSetting(key=SYNC_TOKEN_KEY)
        db.session.add(setting)
    if rotate or not setting.value:
        setting.value = secrets.token_urlsafe(32)
        db.session.commit()
    return setting.value


def _apps_script(token):
    endpoint = current_app.config["WMS_PUBLIC_URL"] + "/production-orders/google-trigger"
    return f'''const WMS_URL = {endpoint!r};
const WMS_TOKEN = {token!r};

function onOpen() {{
  SpreadsheetApp.getUi()
    .createMenu('WMS')
    .addItem('Загрузить заказы в WMS', 'syncWms')
    .addToUi();
}}

function syncWms() {{
  const ui = SpreadsheetApp.getUi();
  const response = UrlFetchApp.fetch(WMS_URL, {{
    method: 'post',
    headers: {{'X-WMS-Sync-Token': WMS_TOKEN}},
    muteHttpExceptions: true
  }});
  const status = response.getResponseCode();
  let result;
  try {{
    result = JSON.parse(response.getContentText());
  }} catch (error) {{
    result = {{message: 'WMS вернула непонятный ответ'}};
  }}
  if (status >= 200 && status < 300 && result.ok) {{
    ui.alert('Готово', result.message, ui.ButtonSet.OK);
  }} else {{
    ui.alert('Не удалось загрузить данные', result.message || ('Ошибка ' + status), ui.ButtonSet.OK);
  }}
}}
'''


def _cell_text(row, column):
    if not column:
        return None
    value = row.get(column)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_DATE_FORMATS = ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def _cell_datetime(row, column):
    text = _cell_text(row, column)
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _cell_date(row, column):
    value = _cell_datetime(row, column)
    return value.date() if value else None


def _advance_stage(order, stage, now):
    """Проставляет отметку времени этому и всем более ранним этапам, у
    которых ее еще нет (см. ProductionOrder — при первой синхронизации
    заказа, уже находящегося не на первом этапе, более ранние этапы
    задним числом не восстановить, поэтому им честно ставится тот же
    момент — "мы увидели заказ здесь", а не придуманная дата). Уже
    проставленные даты не трогает — в т.ч. после переделки, когда заказ
    возвращается на "sample_sewing" и потом снова доходит до более
    позднего этапа: время первого прохождения этапа остается как было."""
    stage_index = PRODUCTION_ORDER_STAGE_KEYS.index(stage)
    for key in PRODUCTION_ORDER_STAGE_KEYS[: stage_index + 1]:
        if order.stage_timestamp(key) is None:
            order.set_stage_timestamp(key, now)
    order.current_stage = stage


def _sync_sheet_rows(headers, rows, now, sheet_anchor_date=None):
    """Разбирает строки ОДНОГО листа и заводит/обновляет ProductionOrder —
    общая логика для режима "один лист по gid" и "все листы таблицы" (см.
    sync_production_orders). sheet_anchor_date — дата из НАЗВАНИЯ листа
    (см. чат), точка отсчета первого этапа ("Поиск поставщика/цеха"), а не
    отдельный статус в таблице. Возвращает (columns, created, updated,
    skipped, unmatched_statuses, cancelled, reworked) для диагностики."""
    columns = map_columns(headers)
    created = 0
    updated = 0
    skipped = 0
    cancelled = 0
    reworked = 0
    unmatched_statuses = []
    anchor_dt = datetime.combine(sheet_anchor_date, datetime.min.time()) if sheet_anchor_date else None

    for row in rows:
        order_number = _cell_text(row, columns["order_number"])
        if not order_number:
            skipped += 1
            continue

        order = ProductionOrder.query.filter_by(order_number=order_number).first()
        if order is None:
            order = ProductionOrder(order_number=order_number, created_at=now)
            db.session.add(order)
            created += 1
        else:
            updated += 1

        marketplace = _cell_text(row, columns["marketplace"])
        if marketplace:
            order.marketplace = marketplace

        raw_status = _cell_text(row, columns["status"])
        order.raw_status = raw_status

        if anchor_dt is not None and order.workshop_search_started_at is None:
            order.workshop_search_started_at = anchor_dt

        for stage_key, date_col in columns["stage_dates"].items():
            value = _cell_datetime(row, date_col)
            if value is not None and order.stage_timestamp(stage_key) is None:
                order.set_stage_timestamp(stage_key, value)

        deadline_col = columns.get("deadline")
        if deadline_col:
            deadline_value = _cell_date(row, deadline_col)
            if deadline_value is not None:
                # В отличие от дат этапов — дедлайн может сдвинуться
                # менеджером, поэтому берем актуальное значение каждый раз,
                # а не только если было пусто.
                order.deadline_date = deadline_value

        kind, stage = classify_status(raw_status)
        if kind == "cancelled":
            if order.sample_cancelled_at is None:
                order.sample_cancelled_at = now
            order.current_stage = PRODUCTION_ORDER_STAGE_CANCELLED
            cancelled += 1
        elif kind == "rework":
            order.rework_count = (order.rework_count or 0) + 1
            order.last_rework_at = now
            if order.sample_sewing_started_at is None:
                order.sample_sewing_started_at = now
            order.current_stage = "sample_sewing"
            reworked += 1
        elif kind == "stage":
            _advance_stage(order, stage, now)
        else:
            if raw_status:
                unmatched_statuses.append(raw_status)

        order.last_synced_at = now

    return columns, created, updated, skipped, unmatched_statuses, cancelled, reworked


def _build_diagnostics(sheet_reports):
    """sheet_reports — список {title, anchor_date, headers, columns,
    rows_count, created, updated, skipped, unmatched_statuses, cancelled,
    reworked}, один элемент на прочитанный лист (несколько — в режиме "все
    листы таблицы", см. sync_production_orders)."""
    lines = []
    total_created = sum(r["created"] for r in sheet_reports)
    total_updated = sum(r["updated"] for r in sheet_reports)
    total_skipped = sum(r["skipped"] for r in sheet_reports)
    total_cancelled = sum(r["cancelled"] for r in sheet_reports)
    total_reworked = sum(r["reworked"] for r in sheet_reports)
    lines.append(
        f"Прочитано листов: {len(sheet_reports)} — "
        + ", ".join(f'"{r["title"]}"' for r in sheet_reports)
    )
    lines.append(
        f"Всего строк обработано: {sum(r['rows_count'] for r in sheet_reports)} "
        f"(создано заказов {total_created}, обновлено {total_updated}, "
        f"без номера заказа пропущено {total_skipped}, отменено образцов "
        f"{total_cancelled}, отправлено на переделку {total_reworked})"
    )
    for report in sheet_reports:
        columns = report["columns"]
        lines.append(f"--- Лист \"{report['title']}\" ---")
        lines.append(
            "Дата из названия листа (точка отсчета) -> "
            + (report["anchor_date"].strftime("%d.%m.%Y") if report["anchor_date"] else "не найдена в названии листа")
        )
        lines.append(f"Заголовки ({len(report['headers'])}): " + (", ".join(report["headers"]) or "не найдены"))
        lines.append(
            "№ заказа -> "
            + (f'"{columns["order_number"]}"' if columns["order_number"] else "НЕ НАЙДЕНА — заказы без номера пропускаются")
        )
        lines.append("Маркетплейс -> " + (f'"{columns["marketplace"]}"' if columns["marketplace"] else "не найдена (необязательно)"))
        lines.append(
            "Статус/этап -> "
            + (f'"{columns["status"]}"' if columns["status"] else "НЕ НАЙДЕНА — этап не будет определяться по статусу")
        )
        lines.append(
            "Дедлайн -> "
            + (f'"{columns["deadline"]}"' if columns.get("deadline") else "не найдена (необязательно)")
        )
        found_dates = [
            f"{PRODUCTION_ORDER_STAGE_LABELS[stage]} -> \"{col}\""
            for stage, col in columns["stage_dates"].items()
            if col
        ]
        if found_dates:
            lines.append("Даты этапов найдены напрямую: " + "; ".join(found_dates))
        if report["unmatched_statuses"]:
            sample = ", ".join(f'"{s}"' for s in report["unmatched_statuses"][:10])
            lines.append(
                f"Нераспознанные статусы ({len(report['unmatched_statuses'])} строк): {sample}"
                + (" ..." if len(report["unmatched_statuses"]) > 10 else "")
            )
    return "\n".join(lines)


def sync_production_orders():
    """Пустой gid в настройках (см. settings()) означает «читать ВСЕ листы
    таблицы», а не один конкретный — на случай, если заказы разложены по
    нескольким листам (периодам/партиям), см. чат. Заказы сопоставляются
    между листами по номеру (order_number), так что один и тот же заказ,
    встреченный на двух листах, не задвоится — просто обновится дважды."""
    # Пока настройки ни разу явно не сохраняли ("Сохранить" не нажимали),
    # в базе еще ничего нет — используем тот же ID по умолчанию, что уже
    # показан подставленным в форме настроек (см. settings()), чтобы кнопка
    # "Синхронизировать сейчас" работала сразу, без обязательного
    # предварительного сохранения формы.
    spreadsheet_id = _get_setting(SHEET_ID_KEY, DEFAULT_SHEET_ID)
    if not spreadsheet_id:
        raise RuntimeError("Не указан ID Google-таблицы с заказами на производство")
    if not google_sheets_configured(current_app):
        raise RuntimeError("В WMS не настроен ключ Google Таблицы (см. страницу «Обмен с 1С» — тот же ключ)")

    gid = _get_setting(SHEET_GID_KEY)
    if gid:
        title = resolve_sheet_title(current_app, spreadsheet_id, gid)
        if not title:
            raise RuntimeError(f"Лист с gid={gid} не найден в таблице — проверьте ссылку и доступ сервис-аккаунта")
        titles = [title]
    else:
        titles = list_sheet_titles(current_app, spreadsheet_id)
        if not titles:
            raise RuntimeError("В таблице не найдено ни одного листа")

    now = datetime.utcnow()
    sheet_reports = []

    # Одним HTTP-запросом на ВСЕ листы разом (batchGet) — при нескольких
    # листах (режим "читать все листы таблицы") это принципиально быстрее,
    # чем по отдельному запросу на каждый: настройки таблицы могут иметь
    # много листов (по периодам/партиям), и по одному запросу на лист
    # синхронизация может не уложиться в таймаут веб-сервера.
    tables = read_sheet_tables(current_app, spreadsheet_id, titles)

    for title in titles:
        headers, rows = tables.get(title, ([], []))
        # Точка отсчета первого этапа — дата из НАЗВАНИЯ листа (см. чат),
        # тот же прием, что и для листов плана отгрузок (см.
        # shipment_plan_import.extract_period_start — например "... от
        # 27.08"). Год не указан в названии — берется текущий (см. саму
        # функцию), поэтому вызывается без даты "today" — по умолчанию.
        anchor_date = extract_period_start(title)
        columns, created, updated, skipped, unmatched_statuses, cancelled, reworked = _sync_sheet_rows(
            headers, rows, now, sheet_anchor_date=anchor_date
        )
        sheet_reports.append(
            {
                "title": title,
                "anchor_date": anchor_date,
                "headers": headers,
                "columns": columns,
                "rows_count": len(rows),
                "created": created,
                "updated": updated,
                "skipped": skipped,
                "unmatched_statuses": unmatched_statuses,
                "cancelled": cancelled,
                "reworked": reworked,
            }
        )

    diagnostics = _build_diagnostics(sheet_reports)
    total_created = sum(r["created"] for r in sheet_reports)
    total_updated = sum(r["updated"] for r in sheet_reports)
    _set_setting(SYNC_AT_KEY, now.strftime("%d.%m.%Y %H:%M:%S"))
    _set_setting(SYNC_ERROR_KEY, "")
    _set_setting(DIAGNOSTICS_KEY, diagnostics)
    db.session.commit()
    return total_created, total_updated, diagnostics


@bp.route("/")
def list_orders():
    if not current_user.is_admin:
        flash("Заказы на производство видит только администратор", "danger")
        return redirect(url_for("main.index"))
    orders = ProductionOrder.query.order_by(ProductionOrder.id.desc()).all()
    now = datetime.utcnow()
    rows = []
    for order in orders:
        if order.current_stage == PRODUCTION_ORDER_STAGE_CANCELLED:
            entered_at = order.sample_cancelled_at
            stage_label = "Образец отменен"
        elif order.current_stage:
            entered_at = order.stage_timestamp(order.current_stage)
            stage_label = PRODUCTION_ORDER_STAGE_LABELS.get(order.current_stage, order.raw_status or "—")
        else:
            entered_at = None
            stage_label = order.raw_status or "—"
        days_in_stage = (now - entered_at).days if entered_at else None
        rows.append(
            {
                "order": order,
                "stage_label": stage_label,
                "days_in_stage": days_in_stage,
            }
        )
    return render_template("production_orders/list.html", rows=rows)


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if not current_user.is_admin:
        flash("Настройки заказов на производство доступны только администратору", "danger")
        return redirect(url_for("main.index"))

    if request.method == "POST":
        action = request.form.get("action")
        if action == "save":
            _set_setting(SHEET_ID_KEY, request.form.get("sheet_id", "").strip())
            _set_setting(SHEET_GID_KEY, request.form.get("sheet_gid", "").strip())
            db.session.commit()
            flash("Настройки таблицы сохранены", "success")
        elif action == "rotate_token":
            _get_or_create_token(rotate=True)
            flash("Код кнопки обновлен. Старый код больше не работает.", "success")
        elif action == "sync_now":
            try:
                created, updated, _diag = sync_production_orders()
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                _set_setting(SYNC_ERROR_KEY, str(exc))
                db.session.commit()
                flash(f"Не удалось синхронизировать таблицу: {exc}", "danger")
            else:
                flash(f"Синхронизировано: создано {created}, обновлено {updated}", "success")
        return redirect(url_for("production_orders.settings"))

    token = _get_or_create_token()
    return render_template(
        "production_orders/settings.html",
        sheet_id=_get_setting(SHEET_ID_KEY, DEFAULT_SHEET_ID),
        sheet_gid=_get_setting(SHEET_GID_KEY, DEFAULT_SHEET_GID),
        apps_script=_apps_script(token),
        google_configured=google_sheets_configured(current_app),
        last_sync_at=_get_setting(SYNC_AT_KEY),
        last_error=_get_setting(SYNC_ERROR_KEY),
        diagnostics=_get_setting(DIAGNOSTICS_KEY),
        orders_count=ProductionOrder.query.count(),
    )


@bp.route("/google-trigger", methods=["POST"])
def google_trigger():
    """Ручной запуск из кнопки Apps Script, встроенной в саму таблицу —
    см. _apps_script и settings()/шаблон production_orders/settings.html."""
    expected = AppSetting.query.get(SYNC_TOKEN_KEY)
    supplied = request.headers.get("X-WMS-Sync-Token", "")
    if not expected or not expected.value or not hmac.compare_digest(supplied, expected.value):
        return jsonify(ok=False, message="Кнопка Google Таблицы не авторизована"), 401
    try:
        created, updated, _diag = sync_production_orders()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        _set_setting(SYNC_ERROR_KEY, str(exc))
        db.session.commit()
        current_app.logger.exception("Не удалось синхронизировать заказы на производство по кнопке")
        return jsonify(ok=False, message=f"Не удалось загрузить данные: {exc}"), 500
    return jsonify(ok=True, message=f"Загружено: создано {created}, обновлено {updated}")
