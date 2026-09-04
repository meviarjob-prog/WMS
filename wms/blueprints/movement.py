from datetime import datetime

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import Box, Cell, MovementDocument, MovementLine, Warehouse
from ..utils.excel_io import export_movement_to_excel, timestamp_for_filename
from ..utils.http import content_disposition
from ..utils.numbering import next_number

bp = Blueprint("movement", __name__)


@bp.route("/")
def list_documents():
    documents = MovementDocument.query.order_by(MovementDocument.created_at.desc()).all()
    return render_template("movement/list.html", documents=documents)


@bp.route("/new", methods=["GET", "POST"])
def new_document():
    if request.method == "GET":
        warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.code).all()
        return render_template("movement/new.html", warehouses=warehouses)

    from_warehouse_id = request.form.get("from_warehouse_id", type=int)
    to_warehouse_id = request.form.get("to_warehouse_id", type=int)
    if not from_warehouse_id or not to_warehouse_id:
        flash("Выберите склад-отправитель и склад назначения", "danger")
        return redirect(url_for("movement.new_document"))

    if from_warehouse_id == to_warehouse_id:
        flash("Склад-отправитель и склад назначения не могут совпадать", "danger")
        return redirect(url_for("movement.new_document"))

    doc = MovementDocument(
        number=next_number("movement"),
        from_warehouse_id=from_warehouse_id,
        to_warehouse_id=to_warehouse_id,
        created_by_id=current_user.id,
    )
    db.session.add(doc)
    db.session.commit()
    flash(f"Список перемещения {doc.number} создан — сканируйте короба", "success")
    return redirect(url_for("movement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>")
def detail(doc_id):
    doc = MovementDocument.query.get_or_404(doc_id)
    lines = doc.lines.order_by(MovementLine.id.asc()).all()
    return render_template("movement/detail.html", doc=doc, lines=lines)


@bp.route("/<int:doc_id>/boxes/add", methods=["POST"])
def add_box(doc_id):
    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    box_number = request.form.get("box_number", "").strip()
    box = Box.query.filter_by(box_number=box_number).first()
    if not box:
        flash(f"Короб '{box_number}' не найден", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    if box.warehouse_id != doc.from_warehouse_id:
        flash(
            f"Короб {box.box_number} не на складе-отправителе «{doc.from_warehouse.name}» "
            f"(сейчас на складе «{box.warehouse.name}»).",
            "danger",
        )
        return redirect(url_for("movement.detail", doc_id=doc.id))

    if doc.lines.filter_by(box_id=box.id).first():
        flash(f"Короб {box.box_number} уже в этом списке", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    line = MovementLine(
        document_id=doc.id,
        box_id=box.id,
        from_warehouse_id=box.warehouse_id,
        from_cell_id=box.cell_id,
    )
    db.session.add(line)
    db.session.commit()
    flash(f"Короб {box.box_number} добавлен в список перемещения", "success")
    return redirect(url_for("movement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/lines/<int:line_id>/delete", methods=["POST"])
def delete_line(doc_id, line_id):
    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("movement.detail", doc_id=doc_id))

    line = MovementLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()
    db.session.delete(line)
    db.session.commit()
    return redirect(url_for("movement.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/lines/<int:line_id>/set-cell", methods=["POST"])
def set_cell(doc_id, line_id):
    doc = MovementDocument.query.get_or_404(doc_id)
    line = MovementLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()

    cell_code = request.form.get("cell_code", "").strip()
    if not cell_code:
        line.to_cell_id = None
        db.session.commit()
        return redirect(url_for("movement.detail", doc_id=doc_id))

    cell = Cell.query.filter_by(warehouse_id=doc.to_warehouse_id, code=cell_code).first()
    if not cell:
        flash(f"Ячейка '{cell_code}' не найдена на складе «{doc.to_warehouse.name}»", "danger")
        return redirect(url_for("movement.detail", doc_id=doc_id))

    line.to_cell_id = cell.id
    db.session.commit()
    flash(f"Короб {line.box.box_number}: ячейка назначения — {cell.code}", "success")
    return redirect(url_for("movement.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/complete", methods=["POST"])
def complete(doc_id):
    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    if doc.lines.count() == 0:
        flash("В списке нет коробов", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    for line in doc.lines:
        box = line.box
        box.warehouse_id = doc.to_warehouse_id
        box.cell_id = line.to_cell_id
        box.status = "stored" if line.to_cell_id else "open"

    doc.status = "completed"
    doc.completed_at = datetime.utcnow()
    db.session.commit()
    flash(f"Перемещение {doc.number} завершено — {doc.lines.count()} короб(ов)", "success")
    return redirect(url_for("movement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/export.xlsx")
def export_document(doc_id):
    doc = MovementDocument.query.get_or_404(doc_id)
    data = export_movement_to_excel([doc])
    fname = f"{doc.number}_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


@bp.route("/export.xlsx")
def export_all():
    documents = MovementDocument.query.order_by(MovementDocument.created_at.desc()).all()
    data = export_movement_to_excel(documents)
    fname = f"movements_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )
