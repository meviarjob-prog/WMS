from flask import (
    Blueprint,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from ..extensions import db
from ..models import Box, BoxItem, Cell, Nomenclature, ReceivingDocument, ReceivingLine
from ..utils.excel_io import export_receiving_to_excel, timestamp_for_filename
from ..utils.http import content_disposition
from ..utils.numbering import next_number

bp = Blueprint("receiving", __name__)


@bp.route("/")
def list_documents():
    documents = ReceivingDocument.query.order_by(ReceivingDocument.created_at.desc()).all()
    return render_template("receiving/list.html", documents=documents)


@bp.route("/new", methods=["GET", "POST"])
def new_document():
    from ..models import Warehouse

    if request.method == "GET":
        warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.code).all()
        return render_template("receiving/new.html", warehouses=warehouses)

    warehouse_id = request.form.get("warehouse_id", type=int)
    supplier = request.form.get("supplier", "").strip()

    if not warehouse_id:
        flash("Выберите склад приемки", "danger")
        return redirect(url_for("receiving.new_document"))

    doc = ReceivingDocument(
        number=next_number("receiving"), warehouse_id=warehouse_id, supplier=supplier
    )
    db.session.add(doc)
    db.session.commit()
    flash(f"Документ приемки {doc.number} создан", "success")
    return redirect(url_for("receiving.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>")
def detail(doc_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    unpacked_lines = doc.lines.filter_by(box_id=None).all()
    boxes = doc.boxes.order_by(Box.created_at.asc()).all()
    return render_template(
        "receiving/detail.html", doc=doc, unpacked_lines=unpacked_lines, boxes=boxes
    )


def _add_or_increment_line(doc, nomenclature, qty):
    line = ReceivingLine(
        document_id=doc.id, nomenclature_id=nomenclature.id, qty=qty, box_id=None
    )
    db.session.add(line)
    db.session.commit()
    return line


@bp.route("/<int:doc_id>/lines/add-by-barcode", methods=["POST"])
def add_line_by_barcode(doc_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        return jsonify({"ok": False, "error": "Документ уже завершен"}), 400

    barcode = (request.json or {}).get("barcode", "").strip()
    qty = (request.json or {}).get("qty", 1) or 1
    item = Nomenclature.query.filter_by(barcode=barcode).first()
    if not item:
        return jsonify({"ok": False, "error": f"Товар со штрихкодом '{barcode}' не найден"}), 404

    line = _add_or_increment_line(doc, item, float(qty))
    return jsonify(
        {
            "ok": True,
            "line": {"id": line.id, "name": item.name, "sku": item.sku, "qty": line.qty},
        }
    )


@bp.route("/<int:doc_id>/lines/add", methods=["POST"])
def add_line(doc_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    nomenclature_id = request.form.get("nomenclature_id", type=int)
    qty = request.form.get("qty", type=float) or 1

    item = Nomenclature.query.get(nomenclature_id)
    if not item:
        flash("Товар не найден", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    _add_or_increment_line(doc, item, qty)
    flash(f"Добавлено: {item.name} ({qty} {item.unit})", "success")
    return redirect(url_for("receiving.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/lines/<int:line_id>/delete", methods=["POST"])
def delete_line(doc_id, line_id):
    line = ReceivingLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()
    if line.box_id is not None:
        flash("Нельзя удалить строку, уже упакованную в короб", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))
    db.session.delete(line)
    db.session.commit()
    return redirect(url_for("receiving.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/boxes/create", methods=["POST"])
def create_box(doc_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    box = Box(
        box_number=next_number("box"),
        warehouse_id=doc.warehouse_id,
        receiving_document_id=doc.id,
        status="open",
    )
    db.session.add(box)
    db.session.commit()
    flash(f"Короб {box.box_number} создан", "success")
    return redirect(url_for("receiving.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/lines/<int:line_id>/pack", methods=["POST"])
def pack_line(doc_id, line_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    line = ReceivingLine.query.filter_by(id=line_id, document_id=doc.id).first_or_404()
    box_id = request.form.get("box_id", type=int)
    qty = request.form.get("qty", type=float)

    box = Box.query.filter_by(id=box_id, receiving_document_id=doc.id).first()
    if not box:
        flash("Короб не найден", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    if line.box_id is not None:
        flash("Строка уже упакована", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    if qty is None or qty <= 0 or qty > line.qty:
        qty = line.qty

    box_item = BoxItem(box_id=box.id, nomenclature_id=line.nomenclature_id, qty=qty)
    db.session.add(box_item)

    if qty >= line.qty:
        line.box_id = box.id
    else:
        line.qty -= qty
        packed_line = ReceivingLine(
            document_id=doc.id,
            nomenclature_id=line.nomenclature_id,
            qty=qty,
            box_id=box.id,
        )
        db.session.add(packed_line)

    db.session.commit()
    flash(f"Товар упакован в короб {box.box_number}", "success")
    return redirect(url_for("receiving.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/boxes/<int:box_id>/place", methods=["POST"])
def place_box(doc_id, box_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    box = Box.query.filter_by(id=box_id, receiving_document_id=doc.id).first_or_404()

    cell_code = request.form.get("cell_code", "").strip()
    if not cell_code:
        flash("Укажите или отсканируйте код ячейки", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    cell = Cell.query.filter_by(warehouse_id=doc.warehouse_id, code=cell_code).first()
    if not cell:
        flash(f"Ячейка '{cell_code}' не найдена на складе {doc.warehouse.name}", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    box.cell_id = cell.id
    box.status = "stored"
    db.session.commit()
    flash(f"Короб {box.box_number} размещен в ячейке {cell.code}", "success")
    return redirect(url_for("receiving.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/complete", methods=["POST"])
def complete(doc_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    if doc.lines.filter_by(box_id=None).count() > 0:
        flash("Не все позиции упакованы в короба", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    if doc.boxes.count() == 0:
        flash("Нет коробов для завершения приемки", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    unplaced = [b for b in doc.boxes if b.cell_id is None]
    if unplaced:
        names = ", ".join(b.box_number for b in unplaced)
        flash(f"Не все короба размещены в ячейках: {names}", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    from datetime import datetime

    doc.status = "completed"
    doc.completed_at = datetime.utcnow()
    db.session.commit()
    flash(f"Приемка {doc.number} завершена", "success")
    return redirect(url_for("receiving.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/export.xlsx")
def export_document(doc_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    data = export_receiving_to_excel([doc])
    fname = f"{doc.number}_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )
