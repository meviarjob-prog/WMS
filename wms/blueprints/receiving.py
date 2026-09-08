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
from ..models import Box, BoxItem, Nomenclature, ReceivingDocument, ReceivingLine, UnplacedStock
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

    # "Активный" короб — как и в Размещении: выбирается сканированием/
    # созданием и хранится в адресе страницы (?box=ID), чтобы дальше
    # сканировать в него товар штрихкод за штрихкодом. Пока короб не выбран,
    # приемка работает как раньше — просто по количеству, в неразмещенный
    # остаток.
    active_box = None
    active_box_id = request.args.get("box", type=int)
    if active_box_id:
        active_box = Box.query.filter_by(id=active_box_id, warehouse_id=doc.warehouse_id).first()

    packed_box_ids = {line.box_id for line in lines if line.box_id}
    packed_boxes = (
        Box.query.filter(Box.id.in_(packed_box_ids)).all() if packed_box_ids else []
    )

    return render_template(
        "receiving/detail.html",
        doc=doc,
        lines=lines,
        active_box=active_box,
        packed_boxes=packed_boxes,
    )


def _add_or_increment_line(doc, nomenclature, qty):
    line = ReceivingLine(document_id=doc.id, nomenclature_id=nomenclature.id, qty=qty)
    db.session.add(line)
    db.session.commit()
    return line


def _receive_item_into_box(doc, box, item, qty):
    """Приемка сразу в короб — товар физически упаковывается в момент
    приемки, минуя неразмещенный остаток (см. complete(): строки с box_id
    в него не идут)."""
    box_item = BoxItem.query.filter_by(box_id=box.id, nomenclature_id=item.id).first()
    if box_item:
        box_item.qty += qty
    else:
        box_item = BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty)
        db.session.add(box_item)

    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=qty, box_id=box.id)
    db.session.add(line)
    db.session.commit()
    return line


@bp.route("/<int:doc_id>/boxes/select", methods=["POST"])
def select_box(doc_id):
    """Сканирование/ввод номера короба — берем любой открытый короб этого
    склада, в т.ч. заготовленный заранее массовым созданием и печатью."""
    doc = ReceivingDocument.query.get_or_404(doc_id)
    box_number = request.form.get("box_number", "").strip()
    box = Box.find_by_scanned_code(box_number, warehouse_id=doc.warehouse_id)
    if not box:
        flash(f"Короб '{box_number}' не найден на складе «{doc.warehouse.name}»", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    return redirect(url_for("receiving.detail", doc_id=doc.id, box=box.id))


@bp.route("/<int:doc_id>/boxes/create", methods=["POST"])
def create_box(doc_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    box = Box(box_number=next_number("box"), warehouse_id=doc.warehouse_id, status="open")
    db.session.add(box)
    db.session.commit()
    flash(f"Короб {box.box_number} создан", "success")
    return redirect(url_for("receiving.detail", doc_id=doc.id, box=box.id))


@bp.route("/<int:doc_id>/boxes/<int:box_id>/lines/add-by-barcode", methods=["POST"])
def add_line_to_box_by_barcode(doc_id, box_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        return jsonify({"ok": False, "error": "Документ уже завершен"}), 400

    box = Box.query.filter_by(id=box_id, warehouse_id=doc.warehouse_id).first()
    if not box:
        return jsonify({"ok": False, "error": "Короб не найден"}), 404

    barcode = (request.json or {}).get("barcode", "").strip()
    qty = float((request.json or {}).get("qty", 1) or 1)
    item = Nomenclature.query.filter_by(barcode=barcode).first()
    if not item:
        return jsonify({"ok": False, "error": f"Товар со штрихкодом '{barcode}' не найден"}), 404

    line = _receive_item_into_box(doc, box, item, qty)
    return jsonify(
        {
            "ok": True,
            "line": {"id": line.id, "name": item.name, "sku": item.sku, "qty": line.qty},
        }
    )


@bp.route("/<int:doc_id>/boxes/<int:box_id>/lines/add", methods=["POST"])
def add_line_to_box(doc_id, box_id):
    """Ручное добавление товара в активный короб (поиск "содержит")."""
    doc = ReceivingDocument.query.get_or_404(doc_id)
    box = Box.query.filter_by(id=box_id, warehouse_id=doc.warehouse_id).first()
    if doc.status != "draft" or not box:
        flash("Документ уже завершен или короб не найден", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id, box=box_id))

    nomenclature_id = request.form.get("nomenclature_id", type=int)
    qty = request.form.get("qty", type=float) or 1
    item = Nomenclature.query.get(nomenclature_id)
    if not item:
        flash("Товар не найден", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id, box=box_id))

    _receive_item_into_box(doc, box, item, qty)
    flash(f"В короб {box.box_number} добавлено: {item.name} ({qty} {item.unit})", "success")
    return redirect(url_for("receiving.detail", doc_id=doc.id, box=box_id))


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

    if line.box_id:
        box_item = BoxItem.query.filter_by(box_id=line.box_id, nomenclature_id=line.nomenclature_id).first()
        if box_item:
            box_item.qty += qty - line.qty
            if box_item.qty <= 0:
                db.session.delete(box_item)

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
    if line.box_id:
        box_item = BoxItem.query.filter_by(box_id=line.box_id, nomenclature_id=line.nomenclature_id).first()
        if box_item:
            box_item.qty -= line.qty
            if box_item.qty <= 0:
                db.session.delete(box_item)
    db.session.delete(line)
    db.session.commit()
    return redirect(url_for("receiving.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/delete", methods=["POST"])
def delete_document(doc_id):
    if not current_user.is_admin:
        flash("Удалять документы может только администратор", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Можно удалить только черновик — завершенный документ уже повлиял на остатки", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    number = doc.number
    db.session.delete(doc)
    db.session.commit()
    flash(f"Документ приемки {number} удален", "success")
    return redirect(url_for("receiving.list_documents"))


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
        if line.box_id:
            # Уже физически упаковано в короб во время приемки — минуя
            # неразмещенный остаток. Короб останется без ячейки, пока его
            # не разместят обычным способом через «Размещение».
            continue
        UnplacedStock.add(doc.warehouse_id, line.nomenclature_id, line.qty)

    doc.status = "completed"
    doc.completed_at = datetime.utcnow()
    db.session.commit()
    flash(
        f"Приемка {doc.number} завершена. Товар без короба зачислен в неразмещенный остаток "
        f"склада «{doc.warehouse.name}» — разместите его в короба и ячейки через «Размещение». "
        f"Товар, упакованный в короб прямо при приемке, останется в коробе — его нужно только "
        f"расставить по ячейкам.",
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
