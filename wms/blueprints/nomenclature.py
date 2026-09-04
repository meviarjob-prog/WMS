from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from ..extensions import db
from ..models import Nomenclature
from ..utils.excel_io import (
    build_nomenclature_template,
    export_nomenclature_to_excel,
    import_nomenclature_from_excel,
    timestamp_for_filename,
)
from ..utils.http import content_disposition

bp = Blueprint("nomenclature", __name__)


@bp.route("/")
def list_nomenclature():
    q = request.args.get("q", "").strip()
    query = Nomenclature.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                Nomenclature.name.ilike(like),
                Nomenclature.sku.ilike(like),
                Nomenclature.barcode.ilike(like),
            )
        )
    items = query.order_by(Nomenclature.name).all()
    return render_template("nomenclature/list.html", items=items, q=q)


@bp.route("/create", methods=["POST"])
def create_nomenclature():
    sku = request.form.get("sku", "").strip()
    barcode = request.form.get("barcode", "").strip()
    name = request.form.get("name", "").strip()
    unit = request.form.get("unit", "шт").strip() or "шт"
    description = request.form.get("description", "").strip()
    norm_minutes = request.form.get("norm_minutes", type=float)

    if not sku or not name:
        flash("Укажите артикул и наименование", "danger")
        return redirect(url_for("nomenclature.list_nomenclature"))

    if not barcode:
        barcode = sku

    if Nomenclature.query.filter_by(sku=sku).first():
        flash(f"Товар с артикулом '{sku}' уже существует", "danger")
        return redirect(url_for("nomenclature.list_nomenclature"))

    if Nomenclature.query.filter_by(barcode=barcode).first():
        flash(f"Штрихкод '{barcode}' уже используется", "danger")
        return redirect(url_for("nomenclature.list_nomenclature"))

    item = Nomenclature(
        sku=sku,
        barcode=barcode,
        name=name,
        unit=unit,
        description=description,
        norm_minutes=norm_minutes,
    )
    db.session.add(item)
    db.session.commit()
    flash(f"Товар '{name}' добавлен", "success")
    return redirect(url_for("nomenclature.list_nomenclature"))


@bp.route("/<int:item_id>/norm", methods=["POST"])
def update_norm(item_id):
    """Норма времени на 1 шт для расчета эффективности в модуле «Производство»."""
    item = Nomenclature.query.get_or_404(item_id)
    norm_minutes = request.form.get("norm_minutes", type=float)
    item.norm_minutes = norm_minutes
    db.session.commit()
    flash(f"Норма для «{item.name}» обновлена", "success")
    return redirect(url_for("nomenclature.list_nomenclature", q=request.form.get("q", "")))


@bp.route("/template.xlsx")
def download_template():
    data = build_nomenclature_template()
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=nomenclature_template.xlsx"},
    )


@bp.route("/export.xlsx")
def export_all():
    items = Nomenclature.query.order_by(Nomenclature.name).all()
    data = export_nomenclature_to_excel(items)
    fname = f"nomenclature_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


@bp.route("/import", methods=["GET", "POST"])
def import_nomenclature():
    if request.method == "GET":
        return render_template("nomenclature/import.html")

    file = request.files.get("file")
    if not file or file.filename == "":
        flash("Выберите файл xlsx для импорта", "danger")
        return redirect(url_for("nomenclature.import_nomenclature"))

    result = import_nomenclature_from_excel(file.stream, db, Nomenclature)
    db.session.commit()

    flash(
        f"Импорт завершен: создано {result.created}, обновлено {result.updated}, "
        f"ошибок {len(result.errors)}",
        "success" if not result.errors else "warning",
    )
    return render_template("nomenclature/import.html", result=result)
