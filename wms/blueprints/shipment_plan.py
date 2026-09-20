from datetime import date, datetime, timedelta
import hmac
import json
import os
import secrets
import tempfile
import threading

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user
from sqlalchemy import func, or_

from ..extensions import db
from ..models import (
    Box,
    BoxItem,
    AppSetting,
    MovementDocument,
    MovementLine,
    Nomenclature,
    ProductionRecord,
    ReceivingDocument,
    ReceivingLine,
    ShipmentPlan,
    ShipmentPlanLine,
    UnplacedStock,
    Warehouse,
)
from ..utils.excel_io import export_shipment_plan_to_excel, timestamp_for_filename
from ..utils.http import content_disposition
from ..utils.numbering import next_number
from ..utils.shipment_plan_import import extract_period_start, parse_plan_sheet
from ..utils.google_sheets import (
    google_sheets_configured,
    load_distribution_workbook,
    received_wms_totals,
    write_distribution_facts,
    write_wms_movement_sheet,
)
from .warehouses import default_fulfillment_1c_name

bp = Blueprint("shipment_plan", __name__)

MARKETPLACES = ("ozon", "wb")
MARKETPLACE_LABELS = {"ozon": "ОЗОН", "wb": "ВБ"}
PERIOD_DAYS = 14
GOOGLE_SYNC_AT_KEY = "google_sheets_last_sync_at"
GOOGLE_SYNC_ERROR_KEY = "google_sheets_last_error"
GOOGLE_SYNC_SHEETS_KEY = "google_sheets_last_names"
GOOGLE_SYNC_TOKEN_KEY = "google_sheets_trigger_token"
_google_sync_lock = threading.Lock()

# Этот endpoint вызывается серверами Google Apps Script и поэтому не имеет
# пользовательской cookie WMS. Доступ защищен отдельным длинным токеном.
GOOGLE_SHEETS_PUBLIC_ENDPOINTS = {"shipment_plan.google_trigger"}


def _get_or_create_city_warehouse(marketplace, city_name):
    wh = Warehouse.query.filter_by(marketplace=marketplace, marketplace_city=city_name).first()
    if wh:
        return wh
    wh = Warehouse(
        code=next_number("warehouse"),
        name=f"{MARKETPLACE_LABELS[marketplace]}: {city_name}",
        marketplace=marketplace,
        marketplace_city=city_name,
        # Стартовая догадка склада 1С по городу (см.
        # warehouses.FULFILLMENT_1C_DEFAULTS) — для неизвестных городов
        # останется пустым, администратор донастроит на странице «Настройки».
        fulfillment_1c_name=default_fulfillment_1c_name(city_name),
    )
    db.session.add(wh)
    db.session.flush()
    return wh


def _received_since_by_warehouse_and_item(window_start):
    """{(to_warehouse_id, nomenclature_id): кол-во} — уже ПОДТВЕРЖДЕННАЯ
    приемка перемещением по отгрузкам, сделанным начиная с даты плана.
    Нужно при загрузке НОВОГО плана: _apply_plan полностью заменяет строки
    старой версии плана (plan.lines.delete()), а вместе с ними и
    накопленный fulfilled_qty — без этой подстраховки уже подтвержденное
    по направлению за этот период просто терялось бы (новая строка снова
    начинала бы с нуля), если оно не попало в сам файл плана ("факт") или
    в Google Таблицу (local_received). Приемка за пределами интервала (до
    начала периода или после дедлайна +PERIOD_DAYS) к текущему плану
    отношения не имеет и не учитывается."""
    rows = (
        db.session.query(
            MovementDocument.to_warehouse_id,
            BoxItem.nomenclature_id,
            func.sum(BoxItem.qty),
        )
        .join(MovementLine, MovementLine.document_id == MovementDocument.id)
        .join(BoxItem, BoxItem.box_id == MovementLine.box_id)
        .filter(
            MovementDocument.received_at.isnot(None),
            func.coalesce(
                MovementDocument.completed_at,
                MovementDocument.received_at,
                MovementDocument.created_at,
            ) >= window_start,
        )
        .group_by(MovementDocument.to_warehouse_id, BoxItem.nomenclature_id)
        .all()
    )
    return {(wh_id, nom_id): qty or 0 for wh_id, nom_id, qty in rows}


def _apply_plan(marketplace, parsed, uploaded_by_id=None):
    """Полностью заменяет строки плана этого маркетплейса новыми из файла."""
    plan = ShipmentPlan.query.filter_by(marketplace=marketplace).first()
    if not plan:
        plan = ShipmentPlan(marketplace=marketplace)
        db.session.add(plan)

    plan.sheet_name = parsed.sheet_name
    plan.uploaded_by_id = uploaded_by_id
    plan.uploaded_at = datetime.utcnow()
    row_periods = [row.get("period_start") for row in parsed.rows if row.get("period_start")]
    # Для общей карточки показываем начало самого раннего листа. При этом
    # маршрутизация использует дату каждой строки отдельно.
    plan.period_start = min(row_periods) if row_periods else extract_period_start(parsed.sheet_name)

    plan.lines.delete()

    city_warehouses = {
        city: _get_or_create_city_warehouse(marketplace, city) for city in parsed.cities
    }

    barcodes = {row["barcode"] for row in parsed.rows}
    nomenclature_by_barcode = {
        n.barcode: n
        for n in Nomenclature.query.filter(Nomenclature.barcode.in_(barcodes)).all()
    }
    # Уже подтвержденное приемкой перемещением в WMS — подстраховка от
    # потери fulfilled_qty при замене строк плана (plan.lines.delete() ниже).
    # Факт пересчитывается отдельно для каждой даты листа. Верхней границы
    # нет: все отгрузки начиная с даты листа закрывают его потребность.
    received_by_period = {None: received_wms_totals()}
    for period_start in set(row_periods):
        received_by_period[period_start] = _received_since_by_warehouse_and_item(
            datetime.combine(period_start, datetime.min.time())
        )

    # Один и тот же штрихкод изредка встречается в файле больше одного раза
    # для одного и того же города (дубль строки при ручном ведении таблицы) —
    # схлопываем такие дубли суммированием количества, а не падаем на
    # уникальном ограничении (plan, склад, штрихкод).
    merged = {}
    for row in parsed.rows:
        key = (row["city"], row["barcode"])
        if key in merged:
            merged[key]["qty"] += row["qty"]
            merged[key]["fact"] += row["fact"]
            dates = [d for d in (merged[key].get("period_start"), row.get("period_start")) if d]
            # Если один SKU-город случайно повторяется в листах разных
            # периодов, считаем его частью более нового плана.
            merged[key]["period_start"] = max(dates) if dates else None
        else:
            merged[key] = dict(row)

    created = 0
    unmatched_barcodes = set()
    for row in merged.values():
        nomenclature = nomenclature_by_barcode.get(row["barcode"])
        if nomenclature is None:
            unmatched_barcodes.add(row["barcode"])
        db.session.add(
            ShipmentPlanLine(
                plan=plan,
                warehouse_id=city_warehouses[row["city"]].id,
                nomenclature_id=nomenclature.id if nomenclature else None,
                barcode=row["barcode"],
                article=row["article"],
                size=row["size"],
                planned_qty=row["qty"],
                # Из Google читаем только план. Факт принадлежит WMS и
                # восстанавливается по принятым перемещениям; затем WMS
                # сам записывает его в Google в колонки «отгружено / в пути».
                fulfilled_qty=(
                    received_by_period[row.get("period_start")].get(
                        (city_warehouses[row["city"]].id, nomenclature.id), 0.0
                    )
                    if nomenclature
                    else 0.0
                ),
                period_start=row.get("period_start"),
            )
        )
        created += 1

    return created, len(unmatched_barcodes)


def _set_sync_setting(key, value):
    setting = AppSetting.query.get(key)
    if setting is None:
        setting = AppSetting(key=key)
        db.session.add(setting)
    setting.value = (value or "")[:200]


def _google_sync_status():
    values = {
        row.key: row.value
        for row in AppSetting.query.filter(
            AppSetting.key.in_((GOOGLE_SYNC_AT_KEY, GOOGLE_SYNC_ERROR_KEY, GOOGLE_SYNC_SHEETS_KEY))
        ).all()
    }
    return {
        "configured": google_sheets_configured(current_app),
        "last_sync_at": values.get(GOOGLE_SYNC_AT_KEY),
        "last_error": values.get(GOOGLE_SYNC_ERROR_KEY),
        "sheet_names": values.get(GOOGLE_SYNC_SHEETS_KEY),
    }


def sync_google_plans_and_movements(uploaded_by_id=None):
    """Читает все листы с признаком «Распределение» и публикует в
    отдельный лист агрегированный факт перемещений из WMS.

    Из Google читаем план, а рассчитанный в WMS факт записываем обратно в
    колонки «отгружено / в пути». При импорте эти колонки не используются
    как источник факта, поэтому обратной петли и задвоения нет.
    """
    workbook, sheet_names = load_distribution_workbook(current_app)
    summary = []
    found_any = False
    for marketplace in MARKETPLACES:
        parsed = parse_plan_sheet(workbook, marketplace)
        workbook.seek(0)
        if parsed is None:
            continue
        found_any = True
        created, unmatched = _apply_plan(marketplace, parsed, uploaded_by_id=uploaded_by_id)
        summary.append(
            f"{MARKETPLACE_LABELS[marketplace]}: {created} позиций, "
            f"неизвестных штрихкодов {unmatched}"
        )
    if not found_any:
        raise RuntimeError("В Google Таблице не найдено подходящих данных плана")

    db.session.commit()
    exported = write_wms_movement_sheet(current_app)
    updated_cells = write_distribution_facts(current_app, workbook)
    _set_sync_setting(GOOGLE_SYNC_AT_KEY, datetime.now().strftime("%d.%m.%Y %H:%M:%S"))
    _set_sync_setting(GOOGLE_SYNC_ERROR_KEY, "")
    _set_sync_setting(GOOGLE_SYNC_SHEETS_KEY, ", ".join(sheet_names))
    db.session.commit()
    return summary, sheet_names, exported, updated_cells


def _get_or_create_google_trigger_token(rotate=False):
    setting = AppSetting.query.get(GOOGLE_SYNC_TOKEN_KEY)
    if setting is None:
        setting = AppSetting(key=GOOGLE_SYNC_TOKEN_KEY)
        db.session.add(setting)
    if rotate or not setting.value:
        setting.value = secrets.token_urlsafe(32)
        db.session.commit()
    return setting.value


def _google_apps_script(token):
    endpoint = current_app.config["WMS_PUBLIC_URL"] + "/shipment-plan/google-trigger"
    return f'''const WMS_URL = {endpoint!r};
const WMS_TOKEN = {token!r};

function onOpen() {{
  SpreadsheetApp.getUi()
    .createMenu('WMS')
    .addItem('Загрузить данные в WMS', 'syncWms')
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


def _save_google_credentials(upload):
    data = upload.read(256 * 1024 + 1)
    if not data:
        raise ValueError("Выберите JSON-файл ключа")
    if len(data) > 256 * 1024:
        raise ValueError("Файл ключа слишком большой")
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Выбранный файл не является корректным JSON-ключом") from exc
    required = ("type", "project_id", "private_key", "client_email", "token_uri")
    if payload.get("type") != "service_account" or any(not payload.get(k) for k in required):
        raise ValueError("Это не ключ сервисного аккаунта Google")

    target = current_app.config["GOOGLE_SERVICE_ACCOUNT_FILE"]
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="google-key-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@bp.route("/upload", methods=["GET", "POST"])
def upload():
    if not current_user.is_admin:
        flash("Загружать план отгрузок может только администратор", "danger")
        return redirect(url_for("shipment_plan.dashboard"))

    if request.method == "GET":
        return render_template("shipment_plan/upload.html")

    file = request.files.get("file")
    if not file or file.filename == "":
        flash("Выберите файл xlsx", "danger")
        return redirect(url_for("shipment_plan.upload"))

    data = file.read()
    summary = []
    found_any = False
    for marketplace in MARKETPLACES:
        import io as _io

        parsed = parse_plan_sheet(_io.BytesIO(data), marketplace)
        if parsed is None:
            continue
        found_any = True
        created, unmatched = _apply_plan(marketplace, parsed, uploaded_by_id=current_user.id)
        summary.append(
            f"{MARKETPLACE_LABELS[marketplace]} («{parsed.sheet_name}»): "
            f"{created} позиций, городов {len(parsed.cities)}, "
            f"неизвестных штрихкодов {unmatched}"
        )

    if not found_any:
        flash(
            "В файле не найден ни один лист «Распределение ОЗОН ФБС ...» "
            "или «Распределение ВБ ФБС ...»",
            "danger",
        )
        return redirect(url_for("shipment_plan.upload"))

    db.session.commit()
    flash("План отгрузок обновлен: " + "; ".join(summary), "success")
    return redirect(url_for("shipment_plan.dashboard"))


@bp.route("/sync-google", methods=["POST"])
def sync_google():
    if not current_user.is_admin:
        flash("Синхронизировать Google Таблицу может только администратор", "danger")
        return redirect(url_for("shipment_plan.dashboard"))
    if not _google_sync_lock.acquire(False):
        flash("Синхронизация уже выполняется. Дождитесь ее завершения.", "warning")
        return redirect(url_for("shipment_plan.dashboard"))
    try:
        summary, sheet_names, exported, updated_cells = sync_google_plans_and_movements(
            uploaded_by_id=current_user.id
        )
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        _set_sync_setting(GOOGLE_SYNC_ERROR_KEY, str(exc))
        db.session.commit()
        flash(f"Не удалось синхронизировать Google Таблицу: {exc}", "danger")
    else:
        flash(
            "Google Таблица синхронизирована: "
            + "; ".join(summary)
            + f"; выгружено строк на лист «WMS — перемещения»: {exported}; "
            + f"обновлено ячеек факта: {updated_cells}; "
            + f"листов: {len(sheet_names)}",
            "success",
        )
    finally:
        _google_sync_lock.release()
    return redirect(url_for("shipment_plan.dashboard"))


@bp.route("/google-trigger", methods=["POST"])
def google_trigger():
    """Ручной запуск из привязанного к таблице Google Apps Script."""
    expected = AppSetting.query.get(GOOGLE_SYNC_TOKEN_KEY)
    supplied = request.headers.get("X-WMS-Sync-Token", "")
    if not expected or not expected.value or not hmac.compare_digest(supplied, expected.value):
        return jsonify(ok=False, message="Кнопка Google Таблицы не авторизована"), 401
    if not google_sheets_configured(current_app):
        return jsonify(ok=False, message="В WMS не настроен ключ Google Таблицы"), 503
    if not _google_sync_lock.acquire(False):
        return jsonify(ok=False, message="Синхронизация уже выполняется"), 409
    try:
        summary, sheet_names, exported, updated_cells = sync_google_plans_and_movements()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        _set_sync_setting(GOOGLE_SYNC_ERROR_KEY, str(exc))
        db.session.commit()
        current_app.logger.exception("Не удалось синхронизировать Google Таблицу по кнопке")
        return jsonify(ok=False, message="Не удалось синхронизировать данные. Проверьте WMS."), 500
    finally:
        _google_sync_lock.release()

    message = (
        "; ".join(summary)
        + f"; выгружено строк на лист «WMS — перемещения»: {exported}; "
        + f"обновлено ячеек факта: {updated_cells}; "
        + f"листов: {len(sheet_names)}"
    )
    return jsonify(ok=True, message=message)


@bp.route("/google-button", methods=["GET", "POST"])
def google_button_setup():
    if not current_user.is_admin:
        flash("Настраивать кнопку Google Таблицы может только администратор", "danger")
        return redirect(url_for("shipment_plan.dashboard"))
    action = request.form.get("action") if request.method == "POST" else None
    token = _get_or_create_google_trigger_token(rotate=action == "rotate_token")
    if action == "rotate_token":
        flash("Код кнопки обновлен. Старый код больше не работает.", "success")
        return redirect(url_for("shipment_plan.google_button_setup"))
    if action == "upload_credentials":
        try:
            _save_google_credentials(request.files.get("credentials"))
        except (AttributeError, OSError, ValueError) as exc:
            flash(f"Не удалось сохранить ключ Google: {exc}", "danger")
        else:
            flash("Ключ Google сохранен на сервере. Можно запускать загрузку.", "success")
        return redirect(url_for("shipment_plan.google_button_setup"))
    return render_template(
        "shipment_plan/google_button.html",
        apps_script=_google_apps_script(token),
        google_configured=google_sheets_configured(current_app),
    )


def _sender_warehouse_ids():
    """Склады-отправители — все обычные (не городские склады маркетплейсов)."""
    return [
        wh.id
        for wh in Warehouse.query.filter_by(marketplace=None, is_active=True).all()
    ]


def _stock_by_nomenclature(warehouse_ids):
    """{nomenclature_id: суммарный остаток} по заданным складам — неразмещенный
    остаток плюс товар, упакованный в короба на этих складах (независимо от
    того, размещен ли короб в ячейке)."""
    if not warehouse_ids:
        return {}

    stock = {}
    for nomenclature_id, qty in (
        db.session.query(UnplacedStock.nomenclature_id, func.sum(UnplacedStock.qty))
        .filter(UnplacedStock.warehouse_id.in_(warehouse_ids))
        .group_by(UnplacedStock.nomenclature_id)
        .all()
    ):
        stock[nomenclature_id] = stock.get(nomenclature_id, 0) + (qty or 0)

    for nomenclature_id, qty in (
        db.session.query(BoxItem.nomenclature_id, func.sum(BoxItem.qty))
        .join(Box, BoxItem.box_id == Box.id)
        .filter(Box.warehouse_id.in_(warehouse_ids))
        .group_by(BoxItem.nomenclature_id)
        .all()
    ):
        stock[nomenclature_id] = stock.get(nomenclature_id, 0) + (qty or 0)

    return stock


def _unplaced_by_nomenclature(warehouse_ids):
    """{nomenclature_id: кол-во} товара, который принят, но еще не упакован
    в короб (висит в UnplacedStock) — по сути, "на разбраковке": уже на
    складе, но пока не готов к отгрузке. Отдельно от _stock_by_nomenclature,
    которая считает вообще любой остаток (включая уже упакованный)."""
    if not warehouse_ids:
        return {}
    return {
        nomenclature_id: qty or 0
        for nomenclature_id, qty in (
            db.session.query(UnplacedStock.nomenclature_id, func.sum(UnplacedStock.qty))
            .filter(UnplacedStock.warehouse_id.in_(warehouse_ids))
            .group_by(UnplacedStock.nomenclature_id)
            .all()
        )
    }


def _pending_sorting_by_nomenclature(warehouse_ids):
    """{nomenclature_id: кол-во} фактически принятого товара, который уже
    отправлен на пересчет/разбраковку, но еще не зачислен в UnplacedStock.

    Черновик накладной сюда принципиально не входит: его ``qty`` изначально
    равно заявленному поставщиком количеству, а значит может содержать еще
    не поступивший товар. Для строк из накладной учитываем только позиции с
    отметкой ``confirmed``; добавленные кладовщиком вручную строки уже сами
    являются фактом приемки. На разбраковке вычитаем выделенный брак.
    Упакованные в короб строки уже видны через BoxItem и второй раз не
    считаются."""
    if not warehouse_ids:
        return {}
    rows = (
        db.session.query(
            ReceivingLine.nomenclature_id,
            func.sum(ReceivingLine.qty - ReceivingLine.defect_qty),
        )
        .join(ReceivingDocument, ReceivingDocument.id == ReceivingLine.document_id)
        .filter(
            ReceivingDocument.warehouse_id.in_(warehouse_ids),
            ReceivingDocument.status.in_(("recounting", "sorting")),
            ReceivingDocument.invoice_file_name.isnot(None),
            ReceivingLine.box_id.is_(None),
            or_(ReceivingLine.expected_qty.is_(None), ReceivingLine.confirmed.is_(True)),
        )
        .group_by(ReceivingLine.nomenclature_id)
        .all()
    )
    return {nomenclature_id: qty or 0 for nomenclature_id, qty in rows}


def _production_by_nomenclature(nomenclature_ids, period_start):
    """{nomenclature_id: кол-во} отсканированного на производстве с начала
    периода плана — просто сумма, без вычета уже принятого через Приемку
    (прямой связи между сканом на производстве и конкретной строкой
    приемки в системе нет, а точный remainder было бы сложно и не нужно
    считать — здесь просто информационная цифра "сколько сделано за
    период", а не точный остаток на цеху)."""
    if not nomenclature_ids or not period_start:
        return {}
    rows = (
        db.session.query(ProductionRecord.nomenclature_id, func.count(ProductionRecord.id))
        .filter(
            ProductionRecord.nomenclature_id.in_(nomenclature_ids),
            ProductionRecord.work_date >= period_start,
        )
        .group_by(ProductionRecord.nomenclature_id)
        .all()
    )
    return {nomenclature_id: qty for nomenclature_id, qty in rows}


def _in_transit_by_warehouse_and_item(period_start=None):
    """{(warehouse_id, nomenclature_id): кол-во} товара, уже отправленного
    перемещением на этот склад-город (документ завершен), но еще не
    подтвержденного кнопкой "Принято на складе" — висит как "в пути".
    fulfilled_qty у строки плана дописывается только при приемке (см.
    movement.receive), поэтому без этой раскладки по товарам "Что нужно
    отправить" ниже продолжал бы требовать полное количество по плану, как
    будто ничего еще не выехало — раньше это было видно только в сводке по
    городу целиком, а не по конкретной позиции."""
    query = (
        db.session.query(
            MovementDocument.to_warehouse_id,
            BoxItem.nomenclature_id,
            func.sum(BoxItem.qty),
        )
        .join(MovementLine, MovementLine.document_id == MovementDocument.id)
        .join(BoxItem, BoxItem.box_id == MovementLine.box_id)
        .filter(
            MovementDocument.status == "completed",
            MovementDocument.received_at.is_(None),
            MovementDocument.marketplace_request_created_at.isnot(None),
        )
    )
    if period_start:
        query = query.filter(
            func.coalesce(MovementDocument.completed_at, MovementDocument.created_at)
            >= datetime.combine(period_start, datetime.min.time())
        )
    rows = query.group_by(
        MovementDocument.to_warehouse_id, BoxItem.nomenclature_id
    ).all()
    return {(wh_id, nom_id): qty or 0 for wh_id, nom_id, qty in rows}


def _pace_analysis(plan, total_planned, total_fulfilled):
    """Успеваем ли отгрузить план за 14 дней с даты из названия листа, и
    сколько дней потребуется при сегодняшнем темпе. Темп считается как
    среднее "факт WMS / дней с начала периода": принятое плюс товар, по
    которому уже создана заявка на маркетплейс и который находится в пути."""
    if not plan.period_start:
        return None

    today = date.today()
    days_elapsed = (today - plan.period_start).days
    deadline = plan.period_start + timedelta(days=PERIOD_DAYS)
    days_left = (deadline - today).days
    remaining = max(total_planned - total_fulfilled, 0)

    rate = total_fulfilled / days_elapsed if days_elapsed > 0 else None
    days_needed = remaining / rate if rate and rate > 0 else None
    required_rate = remaining / days_left if days_left > 0 else None

    if remaining <= 0:
        status = "done"
    elif days_left <= 0:
        status = "overdue"
    elif rate is None or rate <= 0:
        status = "no_data"
    elif days_needed <= days_left:
        status = "on_track"
    else:
        status = "behind"

    return {
        "deadline": deadline,
        "days_elapsed": days_elapsed,
        "days_left": days_left,
        "rate": rate,
        "required_rate": required_rate,
        "days_needed": days_needed,
        "remaining": remaining,
        "status": status,
    }


def _dashboard_context():
    plans = {p.marketplace: p for p in ShipmentPlan.query.all()}
    sender_ids = _sender_warehouse_ids()
    stock = _stock_by_nomenclature(sender_ids)
    unplaced_stock = _unplaced_by_nomenclature(sender_ids)
    for nomenclature_id, qty in _pending_sorting_by_nomenclature(sender_ids).items():
        stock[nomenclature_id] = stock.get(nomenclature_id, 0) + qty
        unplaced_stock[nomenclature_id] = unplaced_stock.get(nomenclature_id, 0) + qty
    in_transit_by_period = {}

    marketplaces_data = []
    lines_by_marketplace = {}
    for marketplace in MARKETPLACES:
        plan = plans.get(marketplace)
        if not plan:
            marketplaces_data.append(
                {"marketplace": marketplace, "label": MARKETPLACE_LABELS[marketplace], "plan": None}
            )
            continue

        lines = plan.lines.all()
        lines_by_marketplace[marketplace] = lines

        # После создания заявки на маркетплейс товар считается отгруженным:
        # он уменьшает остаток плана и одновременно показывается отдельно
        # как «В пути», пока склад назначения не подтвердит приемку.
        for line in lines:
            period_start = line.period_start or plan.period_start
            if period_start not in in_transit_by_period:
                in_transit_by_period[period_start] = _in_transit_by_warehouse_and_item(period_start)
            in_transit_by_item = in_transit_by_period[period_start]
            line.in_transit_qty = in_transit_by_item.get((line.warehouse_id, line.nomenclature_id), 0)
            line.fulfilled_with_transit_qty = line.fulfilled_qty + line.in_transit_qty
            line.effective_remaining_qty = max(
                line.planned_qty - line.fulfilled_with_transit_qty,
                0,
            )

        by_warehouse = {}
        for line in lines:
            row = by_warehouse.setdefault(
                line.warehouse_id,
                {
                    "warehouse": line.warehouse,
                    "planned": 0,
                    "fulfilled": 0,
                    "fulfilled_with_transit": 0,
                    "in_transit": 0,
                },
            )
            row["planned"] += line.planned_qty
            row["fulfilled"] += line.fulfilled_qty
            row["fulfilled_with_transit"] += line.fulfilled_with_transit_qty
            row["in_transit"] += line.in_transit_qty
        cities = sorted(by_warehouse.values(), key=lambda r: r["warehouse"].marketplace_city)
        for row in cities:
            row["remaining"] = max(row["planned"] - row["fulfilled_with_transit"], 0)

        # Штрихкоды с невыполненным остатком, для которых нечем отгружать —
        # только для значка-счетчика на карточке; сам список товаров теперь
        # общий для обоих маркетплейсов (см. picking_list ниже), поэтому
        # здесь достаточно посчитать количество, не строя весь список.
        problem_barcodes = {
            line.barcode
            for line in lines
            if line.effective_remaining_qty > 0
            and (line.nomenclature_id is None or stock.get(line.nomenclature_id, 0) <= 0)
        }

        total_planned = sum(line.planned_qty for line in lines)
        total_fulfilled = sum(line.fulfilled_qty for line in lines)
        total_in_transit = sum(row["in_transit"] for row in cities)
        total_fulfilled_with_transit = total_fulfilled + total_in_transit
        pace = _pace_analysis(plan, total_planned, total_fulfilled_with_transit)

        marketplaces_data.append(
            {
                "marketplace": marketplace,
                "label": MARKETPLACE_LABELS[marketplace],
                "plan": plan,
                "cities": cities,
                "problems_count": len(problem_barcodes),
                "total_planned": total_planned,
                "total_fulfilled": total_fulfilled,
                "total_in_transit": total_in_transit,
                # Факт формирует сама WMS: принятое хранится в строках
                # плана, отправленное, но еще не принятое, добавляется один
                # раз из текущих перемещений.
                "total_fulfilled_with_transit": total_fulfilled_with_transit,
                "pace": pace,
            }
        )

    # Общий список товаров сразу по обоим маркетплейсам — артикул, размер,
    # штрихкод и наличие на складе-отправителе почти всегда одни и те же
    # для ОЗОН и ВБ (один и тот же товар торгуется на обеих площадках), а
    # раньше это все дублировалось в двух почти одинаковых таблицах. Теперь
    # одна строка на штрихкод, а города каждого маркетплейса — отдельными
    # блоками колонок (ОЗОН / ВБ) в той же строке.
    products = {}
    for marketplace, lines in lines_by_marketplace.items():
        for line in lines:
            product = products.setdefault(
                line.barcode,
                {
                    "barcode": line.barcode,
                    "article": line.article,
                    "size": line.size,
                    "no_stock": line.nomenclature_id is None
                    or stock.get(line.nomenclature_id, 0) <= 0,
                    # Принято, но еще не упаковано в короб ("на разбраковке") —
                    # отдельно от no_stock: товар физически есть на складе,
                    # просто еще не готов к отгрузке.
                    "unplaced": unplaced_stock.get(line.nomenclature_id, 0)
                    if line.nomenclature_id is not None
                    else 0,
                    # Готово к отгрузке — уже упаковано в короб (независимо от
                    # того, расставлен ли короб по ячейке), в отличие от
                    # "на разбраковке" выше. stock включает и то, и другое.
                    "ready_to_ship": max(
                        stock.get(line.nomenclature_id, 0)
                        - unplaced_stock.get(line.nomenclature_id, 0),
                        0,
                    )
                    if line.nomenclature_id is not None
                    else 0,
                    "ozon": {},
                    "wb": {},
                    "max_remaining": 0,
                    "in_transit_total": 0,
                },
            )
            product[marketplace][line.warehouse.marketplace_city] = line
            # В таблице показываем остаток самого плана, а уже едущий товар
            # — отдельно в скобках. Маршрутизация коробов считает свободную
            # потребность отдельно и вычитает зарезервированные/собранные/
            # отправленные короба (см. movement._committed_by_warehouse_and_item).
            product["max_remaining"] = max(product["max_remaining"], line.remaining_qty())
            product["in_transit_total"] += line.in_transit_qty

    picking_list = sorted(
        (p for p in products.values() if p["max_remaining"] > 0),
        key=lambda p: (p["article"] or "", p["size"] or ""),
    )

    def _city_names(marketplace):
        for m in marketplaces_data:
            if m["marketplace"] == marketplace and m.get("cities"):
                return [row["warehouse"].marketplace_city for row in m["cities"]]
        return []

    ozon_cities = _city_names("ozon")
    wb_cities = _city_names("wb")

    # Итоговая строка над списком "Что нужно отправить" — просто сумма по
    # каждой колонке (На разбраковке/Готово к отгрузке/В пути и каждый
    # город), чтобы сразу видеть общий объем не пролистывая/не считая
    # вручную по строкам.
    picking_totals = {
        "unplaced": sum(p["unplaced"] for p in picking_list),
        "ready_to_ship": sum(p["ready_to_ship"] for p in picking_list),
        "in_transit": sum(p["in_transit_total"] for p in picking_list),
        "ozon": {
            city: sum(
                p["ozon"][city].remaining_qty()
                for p in picking_list
                if city in p["ozon"]
            )
            for city in ozon_cities
        },
        "wb": {
            city: sum(
                p["wb"][city].remaining_qty()
                for p in picking_list
                if city in p["wb"]
            )
            for city in wb_cities
        },
    }

    # Сводка в шапке страницы: "в пути" по каждому маркетплейсу и общим
    # итогом, плюс "на складе" и "на производстве" — эти два уже не по
    # маркетплейсам (один и тот же остаток/цех работает на оба сразу).
    nomenclature_ids = {
        line.nomenclature_id
        for lines in lines_by_marketplace.values()
        for line in lines
        if line.nomenclature_id is not None
    }
    overall_stock = sum(stock.get(nid, 0) for nid in nomenclature_ids)
    period_starts = [p.period_start for p in plans.values() if p.period_start]
    earliest_period_start = min(period_starts) if period_starts else None
    production_by_nomenclature = _production_by_nomenclature(nomenclature_ids, earliest_period_start)
    overall_production = sum(production_by_nomenclature.values())
    overall_in_transit = sum(m.get("total_in_transit", 0) for m in marketplaces_data)

    summary = {
        "in_transit_by_marketplace": [
            {"label": m["label"], "qty": m.get("total_in_transit", 0)}
            for m in marketplaces_data
            if m.get("plan")
        ],
        "total_in_transit": overall_in_transit,
        "total_stock": overall_stock,
        "total_production": overall_production,
    }

    return {
        "marketplaces": marketplaces_data,
        "picking_list": picking_list,
        "picking_totals": picking_totals,
        "ozon_cities": ozon_cities,
        "wb_cities": wb_cities,
        "summary": summary,
    }


@bp.route("/")
def dashboard():
    return render_template(
        "shipment_plan/dashboard.html",
        **_dashboard_context(),
        google_sync=_google_sync_status(),
    )


@bp.route("/export.xlsx")
def export_all():
    context = _dashboard_context()
    data = export_shipment_plan_to_excel(
        context["picking_list"],
        context["picking_totals"],
        context["ozon_cities"],
        context["wb_cities"],
    )
    fname = f"shipment_plan_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )
