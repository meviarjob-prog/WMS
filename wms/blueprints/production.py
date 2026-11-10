from datetime import date, datetime, timedelta

from flask import Blueprint, Response, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import case, func

from ..extensions import db
from ..models import (
    AppSetting,
    Box,
    BoxItem,
    MVB_ROLES,
    MovementDocument,
    MovementLine,
    Nomenclature,
    PlacementDocument,
    PlacementLine,
    ProductCategory,
    ProductionRecord,
    ReceivingDocument,
    ReceivingLine,
    User,
    WorkEvent,
    WorkPlan,
)
from ..utils.excel_io import export_production_to_excel, timestamp_for_filename
from ..utils.http import content_disposition
from ..utils.work_log import work_day

bp = Blueprint("production", __name__)

SCAN_COOLDOWN_KEY = "production_scan_cooldown_seconds"
DEFAULT_SCAN_COOLDOWN_SECONDS = 40


def get_scan_cooldown_seconds():
    setting = AppSetting.query.get(SCAN_COOLDOWN_KEY)
    if setting and setting.value:
        try:
            return int(setting.value)
        except ValueError:
            pass
    return DEFAULT_SCAN_COOLDOWN_SECONDS


def set_scan_cooldown_seconds(value):
    setting = AppSetting.query.get(SCAN_COOLDOWN_KEY)
    if not setting:
        setting = AppSetting(key=SCAN_COOLDOWN_KEY)
        db.session.add(setting)
    setting.value = str(value)
    db.session.commit()


def _effective_norm_column():
    """Норма для расчета нормо-минут: своя у товара, а если не задана —
    норма его вида (категории)."""
    return func.coalesce(Nomenclature.norm_minutes, ProductCategory.norm_minutes)


def _today_stats(user_id):
    row = (
        db.session.query(
            func.count(ProductionRecord.id),
            func.sum(_effective_norm_column()),
        )
        .join(Nomenclature, Nomenclature.id == ProductionRecord.nomenclature_id)
        .outerjoin(ProductCategory, ProductCategory.id == Nomenclature.category_id)
        .filter(ProductionRecord.user_id == user_id, ProductionRecord.work_date == date.today())
        .first()
    )
    qty, normo_minutes = row if row else (0, None)
    return {"qty": qty or 0, "normo_minutes": normo_minutes or 0}


@bp.route("/")
def index():
    stats = _today_stats(current_user.id)
    records = (
        ProductionRecord.query.filter_by(user_id=current_user.id, work_date=date.today())
        .order_by(ProductionRecord.created_at.desc())
        .limit(50)
        .all()
    )
    return render_template(
        "production/index.html",
        stats=stats,
        records=records,
        cooldown_seconds=get_scan_cooldown_seconds(),
    )


@bp.route("/scan", methods=["POST"])
def scan():
    """Сканируется обычный штрихкод товара. Он одинаков у всех единиц
    одного артикула, поэтому надежно отличить одну единицу от другой
    (как раньше делал уникальный код Честного Знака) нельзя — вместо
    этого простая защита от случайных повторных сканов: минимальный
    интервал между двумя сканами одного сотрудника, задается
    администратором в «Производство → Настройки»."""
    payload = request.json or {}
    barcode = (payload.get("barcode") or "").strip()

    if not barcode:
        return jsonify({"ok": False, "error": "Отсканируйте штрихкод товара"}), 400

    item = Nomenclature.find_by_barcode(barcode)
    if not item:
        return jsonify({"ok": False, "error": f"Товар со штрихкодом '{barcode}' не найден"}), 404

    cooldown = get_scan_cooldown_seconds()
    if cooldown > 0:
        last_record = (
            ProductionRecord.query.filter_by(user_id=current_user.id)
            .order_by(ProductionRecord.created_at.desc())
            .first()
        )
        if last_record:
            elapsed = (datetime.utcnow() - last_record.created_at).total_seconds()
            if elapsed < cooldown:
                wait = int(cooldown - elapsed) + 1
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": f"Слишком быстро — подождите еще {wait} сек с прошлого скана",
                            "wait_seconds": wait,
                        }
                    ),
                    429,
                )

    record = ProductionRecord(
        user_id=current_user.id,
        nomenclature_id=item.id,
        work_date=date.today(),
    )
    db.session.add(record)
    db.session.commit()

    stats = _today_stats(current_user.id)
    return jsonify(
        {
            "ok": True,
            "record": {"id": record.id, "name": item.name, "sku": item.sku, "time": record.created_at.strftime("%H:%M:%S")},
            "today": stats,
            "cooldown_seconds": cooldown,
        }
    )


@bp.route("/records/<int:record_id>/delete", methods=["POST"])
def delete_record(record_id):
    record = ProductionRecord.query.get_or_404(record_id)
    if record.user_id != current_user.id and not current_user.is_admin:
        flash("Можно удалить только свою запись", "danger")
        return redirect(url_for("production.index"))

    db.session.delete(record)
    db.session.commit()
    flash("Запись удалена", "success")
    return redirect(request.referrer or url_for("production.index"))


def _require_admin():
    if not current_user.is_admin:
        flash("Доступно только администраторам", "danger")
        return False
    return True


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if not _require_admin():
        return redirect(url_for("production.index"))

    if request.method == "POST":
        cooldown = request.form.get("cooldown_seconds", type=int)
        if cooldown is None or cooldown < 0:
            flash("Укажите корректную задержку в секундах (0 — без задержки)", "danger")
        else:
            set_scan_cooldown_seconds(cooldown)
            flash("Задержка между сканами обновлена", "success")
        return redirect(url_for("production.settings"))

    categories = ProductCategory.query.order_by(ProductCategory.name).all()
    return render_template(
        "production/settings.html",
        cooldown_seconds=get_scan_cooldown_seconds(),
        categories=categories,
    )


@bp.route("/settings/categories/create", methods=["POST"])
def create_category():
    if not _require_admin():
        return redirect(url_for("production.index"))

    name = request.form.get("name", "").strip()
    keywords = request.form.get("keywords", "").strip()
    norm_minutes = request.form.get("norm_minutes", type=float)

    if not name:
        flash("Укажите название вида товара", "danger")
        return redirect(url_for("production.settings"))

    if ProductCategory.query.filter_by(name=name).first():
        flash(f"Вид «{name}» уже существует", "danger")
        return redirect(url_for("production.settings"))

    category = ProductCategory(name=name, keywords=keywords, norm_minutes=norm_minutes)
    db.session.add(category)
    db.session.commit()
    flash(f"Вид «{name}» добавлен", "success")
    return redirect(url_for("production.settings"))


@bp.route("/settings/categories/<int:category_id>/update", methods=["POST"])
def update_category(category_id):
    if not _require_admin():
        return redirect(url_for("production.index"))

    category = ProductCategory.query.get_or_404(category_id)
    category.keywords = request.form.get("keywords", "").strip()
    category.norm_minutes = request.form.get("norm_minutes", type=float)
    category.box_qty_warning = request.form.get("box_qty_warning", type=float)
    db.session.commit()
    flash(f"Вид «{category.name}» обновлен", "success")
    return redirect(url_for("production.settings"))


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _efficiency_rows(date_from=None, date_to=None, user_id=None):
    effective_norm = _effective_norm_column()
    query = (
        db.session.query(
            ProductionRecord.user_id,
            ProductionRecord.work_date,
            func.count(ProductionRecord.id),
            func.sum(effective_norm),
            func.sum(case((effective_norm.is_(None), 1), else_=0)),
        )
        .join(Nomenclature, Nomenclature.id == ProductionRecord.nomenclature_id)
        .outerjoin(ProductCategory, ProductCategory.id == Nomenclature.category_id)
        .group_by(ProductionRecord.user_id, ProductionRecord.work_date)
    )
    if date_from:
        query = query.filter(ProductionRecord.work_date >= date_from)
    if date_to:
        query = query.filter(ProductionRecord.work_date <= date_to)
    if user_id:
        query = query.filter(ProductionRecord.user_id == user_id)
    query = query.order_by(ProductionRecord.work_date.desc())

    users_by_id = {u.id: u for u in User.query.all()}
    rows = []
    for uid, work_date, qty, normo_minutes, missing_norm in query.all():
        user = users_by_id.get(uid)
        normo_minutes = normo_minutes or 0
        shift_minutes = user.shift_minutes if user else 480
        efficiency = (normo_minutes / shift_minutes * 100) if shift_minutes else None
        rows.append(
            {
                "user": user,
                "work_date": work_date,
                "qty": qty,
                "normo_minutes": round(normo_minutes, 1),
                "missing_norm": missing_norm or 0,
                "shift_minutes": shift_minutes,
                "efficiency": round(efficiency, 1) if efficiency is not None else None,
            }
        )
    return rows


@bp.route("/efficiency")
def efficiency():
    date_from = _parse_date(request.args.get("date_from")) or (date.today() - timedelta(days=7))
    date_to = _parse_date(request.args.get("date_to")) or date.today()
    user_id = request.args.get("user_id", type=int)

    rows = _efficiency_rows(date_from, date_to, user_id)
    users = User.query.order_by(User.username).all()
    return render_template(
        "production/efficiency.html",
        rows=rows,
        users=users,
        date_from=date_from,
        date_to=date_to,
        user_id=user_id,
    )


@bp.route("/efficiency.xlsx")
def efficiency_export():
    date_from = _parse_date(request.args.get("date_from")) or (date.today() - timedelta(days=7))
    date_to = _parse_date(request.args.get("date_to")) or date.today()
    user_id = request.args.get("user_id", type=int)

    rows = _efficiency_rows(date_from, date_to, user_id)
    data = export_production_to_excel(rows)
    fname = f"production_efficiency_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


# --- Выработка сотрудников -------------------------------------------------

WORK_KINDS = (
    ("pack", "Упаковка", "Сколько товара положил в короба и в скольких коробах"),
    ("pick", "Сборка на отгрузку", "Сколько коробов собрал в перемещения"),
)


def _parse_day(value):
    return _parse_date(value)


def _percent(fact, plan):
    if not plan:
        return None
    return round(fact / plan * 100)


def _dashboard_payload(date_from, date_to, facts, users, defaults, overrides):
    """Данные для графиков страницы (рисуются в браузере, переключение
    «упаковка / сборка» без перезагрузки): даты периода, факт по
    сотрудникам и дням [шт, коробов], планы [шт, коробов]."""
    days = []
    day = date_from
    while day <= date_to:
        days.append(day)
        day += timedelta(days=1)
    payload = {
        "dates": [d.isoformat() for d in days],
        "users": {str(uid): u.display_name() for uid, u in users.items()},
        "data": {"pack": {}, "pick": {}},
        "plans": {"pack": {}, "pick": {}},
    }
    for (kind, uid, day), (qty, boxes) in facts.items():
        if uid in users:
            payload["data"][kind].setdefault(str(uid), {})[day.isoformat()] = [round(qty, 3), boxes]
    for (kind, uid), plan in defaults.items():
        if uid in users:
            payload["plans"][kind].setdefault(str(uid), {})["def"] = [plan.qty_plan, plan.boxes_plan]
    for (kind, uid, day), plan in overrides.items():
        if uid in users:
            payload["plans"][kind].setdefault(str(uid), {}).setdefault("days", {})[day.isoformat()] = [
                plan.qty_plan, plan.boxes_plan,
            ]
    return payload


def _history_events_in_range(date_from, date_to):
    return WorkEvent.query.filter(
        WorkEvent.ref_kind == "history",
        WorkEvent.work_date >= date_from,
        WorkEvent.work_date <= date_to,
    ).count()


def _employee_output_context(date_from, date_to, user_id):
    events = (
        db.session.query(
            WorkEvent.kind,
            WorkEvent.user_id,
            WorkEvent.work_date,
            db.func.sum(WorkEvent.qty),
            db.func.count(db.func.distinct(WorkEvent.box_id)),
        )
        .filter(WorkEvent.work_date >= date_from, WorkEvent.work_date <= date_to)
        .group_by(WorkEvent.kind, WorkEvent.user_id, WorkEvent.work_date)
    )
    if user_id:
        events = events.filter(WorkEvent.user_id == user_id)
    facts = {}
    for kind, uid, day, qty, boxes in events.all():
        facts[(kind, uid, day)] = (qty or 0, boxes or 0)

    defaults, overrides = {}, {}
    for plan in WorkPlan.query.all():
        if plan.plan_date is None:
            defaults[(plan.kind, plan.user_id)] = plan
        elif date_from <= plan.plan_date <= date_to:
            overrides[(plan.kind, plan.user_id, plan.plan_date)] = plan

    # Только сотрудники склада: клиенты, водители и операторы МВБ Логистики —
    # другой контур (отдельный вход), в этот отчет они не попадают.
    users = {u.id: u for u in User.query.all() if (u.role or "") not in MVB_ROLES or u.is_admin}
    sections = []
    for kind, title, hint in WORK_KINDS:
        rows = []
        totals = {}
        for (k, uid, day), (qty, boxes) in sorted(
            ((key, val) for key, val in facts.items() if key[1] in users), key=lambda item: (item[0][2], users[item[0][1]].display_name() if item[0][1] in users else ""),
            reverse=True,
        ):
            if k != kind:
                continue
            default = defaults.get((kind, uid))
            override = overrides.get((kind, uid, day))
            plan = override or default
            plan_qty = plan.qty_plan if plan else None
            plan_boxes = plan.boxes_plan if plan else None
            rows.append({
                "user": users.get(uid), "user_id": uid, "day": day, "qty": qty, "boxes": boxes,
                "plan_qty": plan_qty, "plan_boxes": plan_boxes,
                "override_qty": override.qty_plan if override else None,
                "override_boxes": override.boxes_plan if override else None,
                "pct_qty": _percent(qty, plan_qty), "pct_boxes": _percent(boxes, plan_boxes),
            })
            total = totals.setdefault(uid, {"user": users.get(uid), "qty": 0, "boxes": 0, "days": 0})
            total["qty"] += qty
            total["boxes"] += boxes
            total["days"] += 1
        sections.append({
            "kind": kind, "title": title, "hint": hint, "rows": rows,
            "totals": sorted(totals.values(), key=lambda t: -t["boxes"]),
        })
    return {
        "payload": _dashboard_payload(date_from, date_to, facts, users, defaults, overrides),
        "history_events": _history_events_in_range(date_from, date_to),
        "sections": sections,
        "defaults": defaults,
        "users": sorted((u for u in users.values() if u.is_active_user), key=lambda u: u.display_name().lower()),
    }


@bp.route("/employee-output")
def employee_output():
    if current_user.is_production_only():
        # рядовые сотрудники производства видят только свою выработку
        flash("Выработка всех сотрудников доступна руководителям", "warning")
        return redirect(url_for("production.index"))
    today = work_day()
    date_from = _parse_day(request.args.get("date_from")) or (today - timedelta(days=13))
    date_to = _parse_day(request.args.get("date_to")) or today
    user_id = request.args.get("user_id", type=int)
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    if (date_to - date_from).days > 120:
        date_from = date_to - timedelta(days=120)
        flash("Период ограничен 120 днями", "warning")
    context = _employee_output_context(date_from, date_to, user_id)
    return render_template(
        "production/employee_output.html",
        date_from=date_from, date_to=date_to, user_id=user_id, kinds=WORK_KINDS, timedelta=timedelta, **context,
    )


def _number_or_none(raw, as_int=False):
    raw = (raw or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value < 0:
        return None
    return int(round(value)) if as_int else value


def _save_plan(user_id, kind, plan_date, qty_plan, boxes_plan):
    plan = WorkPlan.query.filter_by(user_id=user_id, kind=kind, plan_date=plan_date).first()
    if qty_plan is None and boxes_plan is None:
        if plan:
            db.session.delete(plan)
        return
    if plan is None:
        plan = WorkPlan(user_id=user_id, kind=kind, plan_date=plan_date)
        db.session.add(plan)
    plan.qty_plan = qty_plan
    plan.boxes_plan = boxes_plan


@bp.route("/employee-output/plans", methods=["POST"])
def employee_output_save_plans():
    """Сохранение планов с формы дашборда. Имена полей:
    def__<kind>__<user_id>__qty|boxes — план по умолчанию (на каждый день),
    day__<kind>__<user_id>__<YYYY-MM-DD>__qty|boxes — на конкретную дату.
    Пустое значение убирает план."""
    back = url_for(
        "production.employee_output",
        date_from=request.form.get("date_from") or None,
        date_to=request.form.get("date_to") or None,
        user_id=request.form.get("user_id") or None,
    )
    if not current_user.is_admin:
        flash("План выработки может менять только администратор", "danger")
        return redirect(back)

    pending = {}
    one_user = request.form.get("one_user", "")
    if one_user.isdigit():
        # форма «План на конкретный день»: сотрудник + вид + дата + значения
        one_date = _parse_day(request.form.get("one_date"))
        one_kind = request.form.get("one_kind")
        if one_date is not None and one_kind in ("pack", "pick"):
            pending[(int(one_user), one_kind, one_date)] = {
                "qty": _number_or_none(request.form.get("one_qty")),
                "boxes": _number_or_none(request.form.get("one_boxes"), as_int=True),
            }
    for name, raw in request.form.items():
        parts = name.split("__")
        if parts[0] == "def" and len(parts) == 4:
            _, kind, uid, field = parts
            plan_date = None
        elif parts[0] == "day" and len(parts) == 5:
            _, kind, uid, day, field = parts
            plan_date = _parse_day(day)
            if plan_date is None:
                continue
        else:
            continue
        if kind not in ("pack", "pick") or field not in ("qty", "boxes") or not uid.isdigit():
            continue
        entry = pending.setdefault((int(uid), kind, plan_date), {})
        entry[field] = _number_or_none(raw, as_int=field == "boxes")

    valid_users = {u.id for u in User.query.filter(User.id.in_({key[0] for key in pending})).all()} if pending else set()
    for (uid, kind, plan_date), fields in pending.items():
        if uid in valid_users:
            _save_plan(uid, kind, plan_date, fields.get("qty"), fields.get("boxes"))
    db.session.commit()
    flash("План выработки сохранен", "success")
    return redirect(back)


@bp.route("/employee-output/backfill", methods=["POST"])
def employee_output_backfill():
    """Приблизительное восстановление истории выработки (до появления
    журнала WorkEvent кто именно упаковывал/собирал нигде не хранилось):
    автор документа приёмки/размещения/перемещения считается тем, кто
    сделал все его строки, дата — дата документа (у строк перемещения —
    время скана). Заполняется только период ДО первой настоящей записи
    журнала; повторный запуск заменяет прежнее восстановление."""
    if not current_user.is_admin:
        flash("Восстанавливать историю может только администратор", "danger")
        return redirect(url_for("production.employee_output"))

    real = db.session.query(db.func.min(WorkEvent.created_at)).filter(
        db.or_(WorkEvent.ref_kind.is_(None), WorkEvent.ref_kind != "history")
    ).scalar()
    cutoff = real or datetime.utcnow()
    WorkEvent.query.filter_by(ref_kind="history").delete(synchronize_session=False)

    rows = []

    def add(kind, user_id, moment, box_id, qty, nomenclature_id=None):
        rows.append({
            "user_id": user_id, "work_date": work_day(moment), "kind": kind, "box_id": box_id,
            "nomenclature_id": nomenclature_id, "qty": qty, "ref_kind": "history", "created_at": moment,
        })

    packed = 0
    for line, doc in (
        db.session.query(ReceivingLine, ReceivingDocument)
        .join(ReceivingDocument, ReceivingLine.document_id == ReceivingDocument.id)
        .filter(ReceivingLine.box_id.isnot(None), ReceivingDocument.created_by_id.isnot(None),
                ReceivingDocument.created_at < cutoff, ReceivingLine.qty > 0)
    ):
        add("pack", doc.created_by_id, doc.created_at, line.box_id, line.qty, line.nomenclature_id)
        packed += 1
    for line, doc in (
        db.session.query(PlacementLine, PlacementDocument)
        .join(PlacementDocument, PlacementLine.document_id == PlacementDocument.id)
        .filter(PlacementLine.box_id.isnot(None), PlacementDocument.created_by_id.isnot(None),
                PlacementDocument.created_at < cutoff, PlacementLine.qty > 0)
    ):
        add("pack", doc.created_by_id, doc.created_at, line.box_id, line.qty, line.nomenclature_id)
        packed += 1

    picked = 0
    movement_lines = (
        db.session.query(MovementLine, MovementDocument)
        .join(MovementDocument, MovementLine.document_id == MovementDocument.id)
        .filter(MovementDocument.created_by_id.isnot(None), MovementLine.scanned_at < cutoff)
        .all()
    )
    box_ids = {line.box_id for line, _ in movement_lines}
    totals = {}
    if box_ids:
        for box_id, qty in (
            db.session.query(BoxItem.box_id, db.func.sum(BoxItem.qty))
            .filter(BoxItem.box_id.in_(box_ids)).group_by(BoxItem.box_id)
        ):
            totals[box_id] = qty or 0
    for line, doc in movement_lines:
        add("pick", doc.created_by_id, line.scanned_at, line.box_id, totals.get(line.box_id, 0))
        rows[-1]["ref_id"] = doc.id
        picked += 1
    # ref_kind="history" у сборки тоже — отличать восстановленное от настоящего
    if rows:
        for row in rows:
            row.setdefault("ref_id", None)
        db.session.bulk_insert_mappings(WorkEvent, rows)
    db.session.commit()
    flash(
        f"История восстановлена приблизительно, по авторам документов: упаковка — {packed} записей, "
        f"сборка — {picked} записей. Настоящий учёт идёт с {work_day(cutoff).strftime('%d.%m.%Y')}.",
        "success",
    )
    return redirect(url_for("production.employee_output"))
