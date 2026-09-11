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


@bp.route("/merge", methods=["POST"])
def merge_documents():
    """Свести несколько параллельных листов (по разным людям/участкам
    одного склада) в один итоговый документ: короба объединяются без
    задвоения (один и тот же короб, отсканированный в двух листах по
    ошибке, учитывается один раз), позиции пересчитываются заново из
    содержимого коробов. Исходные листы помечаются как "merged" и
    остаются в истории — ничего не удаляется."""
    doc_ids = request.form.getlist("doc_ids", type=int)
    if len(doc_ids) < 2:
        flash("Выберите минимум два листа для объединения", "danger")
        return redirect(url_for("inventory.list_documents"))

    docs = InventoryDocument.query.filter(InventoryDocument.id.in_(doc_ids)).all()
    if len(docs) != len(set(doc_ids)):
        flash("Не удалось найти все выбранные листы", "danger")
        return redirect(url_for("inventory.list_documents"))
    if any(d.status != "draft" for d in docs):
        flash("Объединять можно только черновики (не завершенные и не уже объединенные листы)", "danger")
        return redirect(url_for("inventory.list_documents"))
    warehouse_ids = {d.warehouse_id for d in docs}
    if len(warehouse_ids) > 1:
        flash("Выбранные листы относятся к разным складам — объединять можно только листы одного склада", "danger")
        return redirect(url_for("inventory.list_documents"))

    merged = InventoryDocument(
        number=next_number("inventory"),
        warehouse_id=warehouse_ids.pop(),
        created_by_id=current_user.id,
    )
    db.session.add(merged)
    db.session.flush()

    seen_box_ids = set()
    duplicate_box_numbers = []
    for doc in docs:
        for scanned in doc.scanned_boxes:
            if scanned.box_id in seen_box_ids:
                duplicate_box_numbers.append(scanned.box.box_number)
                continue
            seen_box_ids.add(scanned.box_id)
            db.session.add(InventoryScannedBox(document_id=merged.id, box_id=scanned.box_id))
            for box_item in scanned.box.items:
                line = InventoryLine.query.filter_by(
                    document_id=merged.id, nomenclature_id=box_item.nomenclature_id
                ).first()
                if line:
                    line.qty += box_item.qty
                else:
                    line = InventoryLine(
                        document_id=merged.id,
                        nomenclature_id=box_item.nomenclature_id,
                        qty=box_item.qty,
                    )
                    db.session.add(line)
        doc.status = "merged"
        doc.merged_into_id = merged.id

    db.session.commit()

    message = f"Листы объединены в {merged.number}: {len(seen_box_ids)} коробов из {len(docs)} листов"
    if duplicate_box_numbers:
        flash(
            message + f". Внимание: короб(а) {', '.join(duplicate_box_numbers)} "
            "отсканированы в нескольких листах — учтены один раз",
            "warning",
        )
    else:
        flash(message, "success")
    return redirect(url_for("inventory.detail", doc_id=merged.id))


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
    box = Box.find_by_scanned_code(box_number)
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
    box.mark_scanned(current_user)
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
