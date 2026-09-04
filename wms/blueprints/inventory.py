from datetime import datetime

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import Box, InventoryDocument, InventoryLine, InventoryScannedBox, Nomenclature, Warehouse
from ..utils.excel_io import export_inventory_to_excel, timestamp_for_filename
from ..utils.http import content_disposition
from ..utils.numbering import next_number

bp = Blueprint("inventory", __name__)


@bp.route("/")
def list_documents():
    documents = InventoryDocument.query.order_by(InventoryDocument.created_at.desc()).all()
    return render_template("inventory/list.html", documents=documents)


@bp.route("/new", methods=["GET", "POST"])
def new_document():
    if request.method == "GET":
        warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.code).all()
        return render_template("inventory/new.html", warehouses=warehouses)

    warehouse_id = request.form.get("warehouse_id", type=int)
    if not warehouse_id:
        flash("Выберите склад", "danger")
        return redirect(url_for("inventory.new_document"))

    doc = InventoryDocument(
        number=next_number("inventory"),
        warehouse_id=warehouse_id,
        created_by_id=current_user.id,
    )
    db.session.add(doc)
    db.session.commit()
    flash(f"Лист инвентаризации {doc.number} создан — сканируйте короба", "success")
    return redirect(url_for("inventory.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>")
def detail(doc_id):
    doc = InventoryDocument.query.get_or_404(doc_id)
    lines = doc.lines.join(InventoryLine.nomenclature).order_by(Nomenclature.name).all()
    scanned_boxes = doc.scanned_boxes.order_by(InventoryScannedBox.scanned_at.desc()).all()
    return render_template("inventory/detail.html", doc=doc, lines=lines, scanned_boxes=scanned_boxes)


@bp.route("/<int:doc_id>/boxes/add", methods=["POST"])
def add_box(doc_id):
    doc = InventoryDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("inventory.detail", doc_id=doc.id))

    box_number = request.form.get("box_number", "").strip()
    box = Box.query.filter_by(box_number=box_number).first()
    if not box:
        flash(f"Короб '{box_number}' не найден", "danger")
        return redirect(url_for("inventory.detail", doc_id=doc.id))

    if box.warehouse_id != doc.warehouse_id:
        flash(
            f"Короб {box.box_number} не на складе «{doc.warehouse.name}» "
            f"(сейчас на складе «{box.warehouse.name}»)",
            "danger",
        )
        return redirect(url_for("inventory.detail", doc_id=doc.id))

    if InventoryScannedBox.query.filter_by(document_id=doc.id, box_id=box.id).first():
        flash(f"Короб {box.box_number} уже учтен в этом листе", "danger")
        return redirect(url_for("inventory.detail", doc_id=doc.id))

    items = box.items.all()
    for box_item in items:
        line = InventoryLine.query.filter_by(
            document_id=doc.id, nomenclature_id=box_item.nomenclature_id
        ).first()
        if line:
            line.qty += box_item.qty
        else:
            line = InventoryLine(
                document_id=doc.id, nomenclature_id=box_item.nomenclature_id, qty=box_item.qty
            )
            db.session.add(line)

    db.session.add(InventoryScannedBox(document_id=doc.id, box_id=box.id))
    db.session.commit()

    if items:
        flash(f"Короб {box.box_number} учтен: {len(items)} позиция(й)", "success")
    else:
        flash(f"Короб {box.box_number} учтен: короб пуст, товар не добавлен", "warning")
    return redirect(url_for("inventory.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/scanned-boxes/<int:scanned_id>/delete", methods=["POST"])
def delete_scanned_box(doc_id, scanned_id):
    doc = InventoryDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("inventory.detail", doc_id=doc_id))

    scanned = InventoryScannedBox.query.filter_by(id=scanned_id, document_id=doc_id).first_or_404()
    box = scanned.box

    for box_item in box.items:
        line = InventoryLine.query.filter_by(
            document_id=doc.id, nomenclature_id=box_item.nomenclature_id
        ).first()
        if line:
            line.qty -= box_item.qty
            if line.qty <= 0:
                db.session.delete(line)

    db.session.delete(scanned)
    db.session.commit()
    flash(f"Короб {box.box_number} исключен из листа, суммы пересчитаны", "success")
    return redirect(url_for("inventory.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/complete", methods=["POST"])
def complete(doc_id):
    doc = InventoryDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("inventory.detail", doc_id=doc.id))

    if doc.lines.count() == 0:
        flash("В листе нет позиций — отсканируйте хотя бы один короб", "danger")
        return redirect(url_for("inventory.detail", doc_id=doc.id))

    doc.status = "completed"
    doc.completed_at = datetime.utcnow()
    db.session.commit()
    flash(f"Инвентаризация {doc.number} завершена", "success")
    return redirect(url_for("inventory.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/delete", methods=["POST"])
def delete_document(doc_id):
    if not current_user.is_admin:
        flash("Удалять документы может только администратор", "danger")
        return redirect(url_for("inventory.detail", doc_id=doc_id))

    doc = InventoryDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Можно удалить только черновик", "danger")
        return redirect(url_for("inventory.detail", doc_id=doc_id))

    number = doc.number
    db.session.delete(doc)
    db.session.commit()
    flash(f"Лист инвентаризации {number} удален", "success")
    return redirect(url_for("inventory.list_documents"))


@bp.route("/<int:doc_id>/export.xlsx")
def export_document(doc_id):
    doc = InventoryDocument.query.get_or_404(doc_id)
    data = export_inventory_to_excel([doc])
    fname = f"{doc.number}_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


@bp.route("/export.xlsx")
def export_all():
    documents = InventoryDocument.query.order_by(InventoryDocument.created_at.desc()).all()
    data = export_inventory_to_excel(documents)
    fname = f"inventory_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )
