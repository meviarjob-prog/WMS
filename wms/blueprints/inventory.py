from datetime import datetime

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from ..extensions import db
from ..models import (
    Box,
    BoxItem,
    Cell,
    InventoryDocument,
    InventoryLine,
    InventoryScannedBox,
    Nomenclature,
    ReceivingDocument,
    UnplacedStock,
    Warehouse,
    Zone,
)
from ..utils.excel_io import export_inventory_to_excel, timestamp_for_filename
from ..utils.document_access import ensure_view_document_access, owned_query
from ..utils.http import content_disposition
from ..utils.numbering import next_number
from .placement import _place_box

bp = Blueprint("inventory", __name__)


@bp.before_request
def _restrict_document_access():
    ensure_view_document_access(InventoryDocument)


def _warehouse_stock_by_nomenclature(warehouse_id):
    """{nomenclature_id: кол-во} учётного остатка склада на текущий момент —
    неразмещенный остаток плюс товар, упакованный в короба на этом складе
    (независимо от того, размещен ли короб в ячейке). Это то, с чем и
    сравнивается фактический подсчет инвентаризации (см. detail())."""
    stock = {}
    for nomenclature_id, qty in (
        db.session.query(UnplacedStock.nomenclature_id, UnplacedStock.qty)
        .filter_by(warehouse_id=warehouse_id)
        .all()
    ):
        stock[nomenclature_id] = stock.get(nomenclature_id, 0) + (qty or 0)

    for nomenclature_id, qty in (
        db.session.query(BoxItem.nomenclature_id, func.sum(BoxItem.qty))
        .join(Box, BoxItem.box_id == Box.id)
        .filter(Box.warehouse_id == warehouse_id)
        .group_by(BoxItem.nomenclature_id)
        .all()
    ):
        stock[nomenclature_id] = stock.get(nomenclature_id, 0) + (qty or 0)

    return stock


def _cell_stock_by_nomenclature(cell_id):
    """{nomenclature_id: кол-во} того, что по системе СЕЙЧАС физически
    стоит в этой конкретной ячейке — сумма по коробам с cell_id == этой
    ячейке. В отличие от _warehouse_stock_by_nomenclature, неразмещенный
    остаток сюда не входит — он ни к какой ячейке не привязан по
    определению, сравнивать его с содержимым ОДНОЙ ячейки нет смысла."""
    stock = {}
    for nomenclature_id, qty in (
        db.session.query(BoxItem.nomenclature_id, func.sum(BoxItem.qty))
        .join(Box, BoxItem.box_id == Box.id)
        .filter(Box.cell_id == cell_id)
        .group_by(BoxItem.nomenclature_id)
        .all()
    ):
        stock[nomenclature_id] = stock.get(nomenclature_id, 0) + (qty or 0)
    return stock


def _zone_stock_by_nomenclature(zone_id):
    """Аналог _cell_stock_by_nomenclature для выборочной инвентаризации
    целого РЯДА без ячеек (см. чат — помещения, где ячейки завести нельзя):
    сумма по коробам, стоящим в ряду напрямую (Box.zone_id), без учета
    коробов, расставленных в ячейках внутри этого же ряда — этот режим
    предназначен именно для рядов, в которых ячеек нет вовсе."""
    stock = {}
    for nomenclature_id, qty in (
        db.session.query(BoxItem.nomenclature_id, func.sum(BoxItem.qty))
        .join(Box, BoxItem.box_id == Box.id)
        .filter(Box.zone_id == zone_id)
        .group_by(BoxItem.nomenclature_id)
        .all()
    ):
        stock[nomenclature_id] = stock.get(nomenclature_id, 0) + (qty or 0)
    return stock


@bp.route("/")
def list_documents():
    documents = owned_query(InventoryDocument).order_by(InventoryDocument.created_at.desc()).all()
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

    docs = owned_query(InventoryDocument).filter(InventoryDocument.id.in_(doc_ids)).all()
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
    scopes = {(d.cell_id, d.zone_id) for d in docs}
    if len(scopes) > 1:
        flash(
            "Выбранные листы относятся к разным ячейкам/рядам (или к участку и складу целиком) — "
            "объединять можно только листы одного и того же участка",
            "danger",
        )
        return redirect(url_for("inventory.list_documents"))
    cell_id, zone_id = scopes.pop()

    merged = InventoryDocument(
        number=next_number("inventory"),
        warehouse_id=warehouse_ids.pop(),
        cell_id=cell_id,
        zone_id=zone_id,
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

    cell = None
    zone = None
    cell_code = request.form.get("cell_code", "").strip()
    mode = request.form.get("mode")
    # Выборочная инвентаризация по ячейке или по ряду целиком (см. чат —
    # ряд без ячеек, для помещений, где ячейки завести нельзя) — только
    # когда явно выбран режим, чтобы случайно введенный текст в поле (если
    # бы оно было видно всегда) не превращал общую инвентаризацию в
    # выборочную.
    if mode == "cell":
        if not cell_code:
            flash("Укажите код ячейки для выборочной инвентаризации", "danger")
            return redirect(url_for("inventory.new_document"))
        cell = Cell.query.filter_by(warehouse_id=warehouse_id, code=cell_code).first()
        if not cell:
            flash(f"Ячейка «{cell_code}» не найдена на выбранном складе", "danger")
            return redirect(url_for("inventory.new_document"))
    elif mode == "zone":
        if not cell_code:
            flash("Укажите код ряда для выборочной инвентаризации", "danger")
            return redirect(url_for("inventory.new_document"))
        zone = Zone.query.filter_by(warehouse_id=warehouse_id, code=cell_code).first()
        if not zone:
            flash(f"Ряд «{cell_code}» не найден на выбранном складе", "danger")
            return redirect(url_for("inventory.new_document"))

    doc = InventoryDocument(
        number=next_number("inventory"),
        warehouse_id=warehouse_id,
        cell_id=cell.id if cell else None,
        zone_id=zone.id if zone else None,
        created_by_id=current_user.id,
    )
    db.session.add(doc)
    db.session.commit()
    if cell:
        flash(f"Лист инвентаризации {doc.number} создан для ячейки {cell.code} — сканируйте короба", "success")
    elif zone:
        flash(f"Лист инвентаризации {doc.number} создан для ряда {zone.code} — сканируйте короба", "success")
    else:
        flash(f"Лист инвентаризации {doc.number} создан — сканируйте короба", "success")
    return redirect(url_for("inventory.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>")
def detail(doc_id):
    doc = InventoryDocument.query.get_or_404(doc_id)
    lines = doc.lines.join(InventoryLine.nomenclature).order_by(Nomenclature.name).all()
    scanned_boxes = doc.scanned_boxes.order_by(InventoryScannedBox.scanned_at.desc()).all()

    # Сличительная ведомость: учётный остаток склада (сейчас) против того,
    # что реально насчитали в этом документе — по объединению обоих
    # списков товаров, чтобы не пропустить ни то, что есть на складе, но не
    # попало в подсчет, ни то, что посчитали, а на складе по учету нет.
    if doc.cell_id:
        stock_by_item = _cell_stock_by_nomenclature(doc.cell_id)
    elif doc.zone_id:
        stock_by_item = _zone_stock_by_nomenclature(doc.zone_id)
    else:
        stock_by_item = _warehouse_stock_by_nomenclature(doc.warehouse_id)
    counted_by_item = {line.nomenclature_id: line.qty for line in lines}
    nomenclature_ids = set(stock_by_item) | set(counted_by_item)
    nomenclatures = (
        {n.id: n for n in Nomenclature.query.filter(Nomenclature.id.in_(nomenclature_ids)).all()}
        if nomenclature_ids
        else {}
    )
    comparison = sorted(
        (
            {
                "nomenclature": nomenclatures[nid],
                "system_qty": stock_by_item.get(nid, 0),
                "counted_qty": counted_by_item.get(nid, 0),
                "diff": counted_by_item.get(nid, 0) - stock_by_item.get(nid, 0),
            }
            for nid in nomenclature_ids
        ),
        key=lambda row: row["nomenclature"].name,
    )

    empty_box = None
    empty_box_id = request.args.get("empty_box", type=int)
    if empty_box_id and (doc.cell_id or doc.zone_id):
        empty_box = Box.query.filter_by(
            id=empty_box_id, warehouse_id=doc.warehouse_id
        ).first()
    return render_template(
        "inventory/detail.html", doc=doc, lines=lines, scanned_boxes=scanned_boxes,
        comparison=comparison, empty_box=empty_box,
        resume_box_number=request.args.get("resume_box_number", ""),
    )


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

    if (doc.cell_id or doc.zone_id) and box.items.count() == 0:
        flash(f"Короб {box.box_number} пуст. Можно сразу принять товар в него.", "warning")
        return redirect(url_for("inventory.detail", doc_id=doc.id, empty_box=box.id))

    moved_from = None
    if doc.cell_id or doc.zone_id:
        # Выборочная инвентаризация ячейки/ряда — сканирование короба сразу
        # же и есть его фактическое размещение туда (см. чат — ряд без
        # ячеек), без отдельного подтверждения, даже если короб был в
        # другом месте. _place_box сам разбирает, ячейка это или ряд.
        already_here = (doc.cell_id and box.cell_id == doc.cell_id) or (
            doc.zone_id and box.zone_id == doc.zone_id
        )
        if box.is_placed() and not already_here:
            moved_from = f"ячейки {box.cell.code}" if box.cell_id else f"ряда {box.zone.code}"
        target_code = doc.cell.code if doc.cell_id else doc.zone.code
        error = _place_box(box, target_code, doc.warehouse_id)
        if error:
            flash(error, "danger")
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

    move_note = f" (перемещен из {moved_from})" if moved_from else (
        f" (размещен в {box.location_label()})" if (doc.cell_id or doc.zone_id) and not moved_from else ""
    )
    if items:
        flash(f"Короб {box.box_number} учтен{move_note}: {len(items)} позиция(й)", "success")
    else:
        flash(f"Короб {box.box_number} учтен{move_note}: короб пуст, товар не добавлен", "warning")
    return redirect(url_for("inventory.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/empty-box/<int:box_id>/receive", methods=["POST"])
def receive_into_empty_box(doc_id, box_id):
    doc = InventoryDocument.query.get_or_404(doc_id)
    box = Box.query.filter_by(id=box_id, warehouse_id=doc.warehouse_id).first_or_404()
    if doc.status != "draft" or not (doc.cell_id or doc.zone_id) or box.items.count() != 0:
        flash("Короб уже заполнен либо инвентаризация завершена", "danger")
        return redirect(url_for("inventory.detail", doc_id=doc.id))
    receiving = ReceivingDocument(
        number=next_number("receiving"),
        warehouse_id=doc.warehouse_id,
        created_by_id=current_user.id,
        return_inventory_id=doc.id,
        return_inventory_box_id=box.id,
    )
    db.session.add(receiving)
    db.session.commit()
    flash(
        f"Приемка {receiving.number} создана. Сканируйте товар в короб {box.box_number}, затем завершите приемку.",
        "success",
    )
    return redirect(url_for("receiving.detail", doc_id=receiving.id, box=box.id))


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


@bp.route("/<int:doc_id>/lines/add", methods=["POST"])
def add_line(doc_id):
    """Учет товара напрямую, без короба — для неразмещенного остатка,
    который лежит россыпью и никогда не попадет в подсчет через
    сканирование коробов (см. detail() и почему такой товар иначе всегда
    показывался бы недостачей в сличительной ведомости)."""
    doc = InventoryDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("inventory.detail", doc_id=doc.id))

    nomenclature_id = request.form.get("nomenclature_id", type=int)
    qty = request.form.get("qty", type=float)
    item = Nomenclature.query.get(nomenclature_id) if nomenclature_id else None
    if not item or not qty or qty <= 0:
        flash("Укажите товар и корректное количество", "danger")
        return redirect(url_for("inventory.detail", doc_id=doc.id))

    line = InventoryLine.query.filter_by(document_id=doc.id, nomenclature_id=item.id).first()
    if line:
        line.qty += qty
    else:
        line = InventoryLine(document_id=doc.id, nomenclature_id=item.id, qty=qty)
        db.session.add(line)

    db.session.commit()
    flash(f"Учтено без короба: {item.name} — {qty} {item.unit}", "success")
    return redirect(url_for("inventory.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/lines/<int:line_id>/update", methods=["POST"])
def update_line(doc_id, line_id):
    """Ручная правка итоговой строки — например, поправить сумму, если
    неразмещенный товар посчитали неточно или короб исключили, а строку
    (в т.ч. ее часть, добавленную вручную) нужно скорректировать отдельно."""
    doc = InventoryDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("inventory.detail", doc_id=doc_id))

    line = InventoryLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()
    qty = request.form.get("qty", type=float)
    if qty is None or qty < 0:
        flash("Укажите корректное количество", "danger")
        return redirect(url_for("inventory.detail", doc_id=doc_id))

    if qty == 0:
        db.session.delete(line)
    else:
        line.qty = qty
    db.session.commit()
    return redirect(url_for("inventory.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/lines/<int:line_id>/delete", methods=["POST"])
def delete_line(doc_id, line_id):
    doc = InventoryDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("inventory.detail", doc_id=doc_id))

    line = InventoryLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()
    db.session.delete(line)
    db.session.commit()
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
    documents = owned_query(InventoryDocument).order_by(InventoryDocument.created_at.desc()).all()
    data = export_inventory_to_excel(documents)
    fname = f"inventory_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )
