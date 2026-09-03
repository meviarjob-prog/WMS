from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from ..extensions import db
from ..models import Box, Cell, MovementDocument, Warehouse
from ..utils.excel_io import export_movement_to_excel, timestamp_for_filename
from ..utils.numbering import next_number

bp = Blueprint("movement", __name__)


@bp.route("/")
def list_documents():
    documents = MovementDocument.query.order_by(MovementDocument.created_at.desc()).all()
    return render_template("movement/list.html", documents=documents)


@bp.route("/new", methods=["GET", "POST"])
def new_movement():
    warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.code).all()

    if request.method == "GET":
        return render_template("movement/new.html", warehouses=warehouses)

    box_number = request.form.get("box_number", "").strip()
    to_warehouse_id = request.form.get("to_warehouse_id", type=int)
    to_cell_code = request.form.get("to_cell_code", "").strip()

    box = Box.query.filter_by(box_number=box_number).first()
    if not box:
        flash(f"Короб '{box_number}' не найден", "danger")
        return render_template("movement/new.html", warehouses=warehouses)

    if not to_warehouse_id:
        flash("Выберите склад назначения", "danger")
        return render_template("movement/new.html", warehouses=warehouses)

    to_cell = None
    if to_cell_code:
        to_cell = Cell.query.filter_by(warehouse_id=to_warehouse_id, code=to_cell_code).first()
        if not to_cell:
            flash(f"Ячейка '{to_cell_code}' не найдена на выбранном складе", "danger")
            return render_template("movement/new.html", warehouses=warehouses)

    doc = MovementDocument(
        number=next_number("movement"),
        box_id=box.id,
        from_warehouse_id=box.warehouse_id,
        from_cell_id=box.cell_id,
        to_warehouse_id=to_warehouse_id,
        to_cell_id=to_cell.id if to_cell else None,
    )
    db.session.add(doc)

    box.warehouse_id = to_warehouse_id
    box.cell_id = to_cell.id if to_cell else None
    box.status = "stored" if to_cell else "open"

    db.session.commit()
    flash(f"Перемещение {doc.number} выполнено", "success")
    return redirect(url_for("movement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>")
def detail(doc_id):
    doc = MovementDocument.query.get_or_404(doc_id)
    return render_template("movement/detail.html", doc=doc)


@bp.route("/export.xlsx")
def export_all():
    documents = MovementDocument.query.order_by(MovementDocument.created_at.desc()).all()
    data = export_movement_to_excel(documents)
    fname = f"movements_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )
