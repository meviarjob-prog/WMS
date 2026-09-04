from datetime import datetime

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
from flask_login import current_user

from ..extensions import db
from ..models import Nomenclature, ReceivingDocument, ReceivingLine, UnplacedStock
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
        number=next_number("receiving"),
        warehouse_id=warehouse_id,
        supplier=supplier,
        created_by_id=current_user.id,
    )
    db.session.add(doc)
    db.session.commit()
    flash(f"Документ приемки {doc.number} создан", "success")
    return redirect(url_for("receiving.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>")
def detail(doc_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    lines = doc.lines.all()
    return render_template("receiving/detail.html", doc=doc, lines=lines)


def _add_or_increment_line(doc, nomenclature, qty):
    line = ReceivingLine(document_id=doc.id, nomenclature_id=nomenclature.id, qty=qty)
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


@bp.route("/<int:doc_id>/lines/<int:line_id>/update", methods=["POST"])
def update_line(doc_id, line_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    line = ReceivingLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()
    qty = request.form.get("qty", type=float)
    if qty is None or qty <= 0:
        flash("Укажите корректное количество", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    line.qty = qty
    db.session.commit()
    flash(f"Количество обновлено: {line.nomenclature.name} — {qty} {line.nomenclature.unit}", "success")
    return redirect(url_for("receiving.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/lines/<int:line_id>/delete", methods=["POST"])
def delete_line(doc_id, line_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    line = ReceivingLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()
    db.session.delete(line)
    db.session.commit()
    return redirect(url_for("receiving.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/complete", methods=["POST"])
def complete(doc_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    if doc.lines.count() == 0:
        flash("В документе нет позиций", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    for line in doc.lines:
        UnplacedStock.add(doc.warehouse_id, line.nomenclature_id, line.qty)

    doc.status = "completed"
    doc.completed_at = datetime.utcnow()
    db.session.commit()
    flash(
        f"Приемка {doc.number} завершена. Товар зачислен в неразмещенный остаток "
        f"склада «{doc.warehouse.name}» — разместите его в короба и ячейки через «Размещение».",
        "success",
    )
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
