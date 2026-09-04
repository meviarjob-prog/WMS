from datetime import date, datetime, timedelta

from flask import Blueprint, Response, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import case, func

from ..extensions import db
from ..models import AppSetting, Nomenclature, ProductCategory, ProductionRecord, User
from ..utils.excel_io import export_production_to_excel, timestamp_for_filename
from ..utils.http import content_disposition

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

    item = Nomenclature.query.filter_by(barcode=barcode).first()
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
