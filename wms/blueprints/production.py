from datetime import date, datetime, timedelta

from flask import Blueprint, Response, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import case, func

from ..extensions import db
from ..models import Nomenclature, ProductionRecord, User
from ..utils.excel_io import export_production_to_excel, timestamp_for_filename
from ..utils.http import content_disposition

bp = Blueprint("production", __name__)


def _today_stats(user_id):
    row = (
        db.session.query(
            func.count(ProductionRecord.id),
            func.sum(Nomenclature.norm_minutes),
        )
        .join(Nomenclature, Nomenclature.id == ProductionRecord.nomenclature_id)
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
    return render_template("production/index.html", stats=stats, records=records)


@bp.route("/scan", methods=["POST"])
def scan():
    payload = request.json or {}
    barcode = (payload.get("barcode") or "").strip()
    chestny_znak = (payload.get("chestny_znak") or "").strip()

    if not barcode or not chestny_znak:
        return jsonify({"ok": False, "error": "Отсканируйте и штрихкод товара, и код Честного Знака"}), 400

    item = Nomenclature.query.filter_by(barcode=barcode).first()
    if not item:
        return jsonify({"ok": False, "error": f"Товар со штрихкодом '{barcode}' не найден"}), 404

    existing = ProductionRecord.query.filter_by(chestny_znak=chestny_znak).first()
    if existing:
        when = existing.created_at.strftime("%d.%m.%Y %H:%M")
        who = existing.user.display_name() if existing.user else "неизвестно"
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"Этот код Честного Знака уже учтен: {who}, {when}. Повторно засчитать нельзя.",
                }
            ),
            409,
        )

    record = ProductionRecord(
        user_id=current_user.id,
        nomenclature_id=item.id,
        chestny_znak=chestny_znak,
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


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _efficiency_rows(date_from=None, date_to=None, user_id=None):
    query = (
        db.session.query(
            ProductionRecord.user_id,
            ProductionRecord.work_date,
            func.count(ProductionRecord.id),
            func.sum(Nomenclature.norm_minutes),
            func.sum(case((Nomenclature.norm_minutes.is_(None), 1), else_=0)),
        )
        .join(Nomenclature, Nomenclature.id == ProductionRecord.nomenclature_id)
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
