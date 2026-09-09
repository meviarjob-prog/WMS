from datetime import datetime

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import and_, func, or_

from ..extensions import db
from ..models import (
    Box,
    BoxItem,
    Cell,
    CELL_CAPACITY,
    MovementDocument,
    MovementLine,
    ShipmentPlanLine,
    Warehouse,
)
from ..utils.excel_io import export_movement_to_excel, timestamp_for_filename
from ..utils.http import content_disposition
from ..utils.numbering import next_number
from ..utils.waybill_pdf import build_movement_waybills_pdf

bp = Blueprint("movement", __name__)


def _apply_shipment_fulfillment(box, warehouse_id):
    """Если короб приехал на склад-город из плана отгрузок (см.
    shipment_plan) — дописывает выполнение плана по товарам в этом коробе.
    Для обычных складов (не из плана) не находит ни одной строки — no-op."""
    for item in box.items:
        plan_line = ShipmentPlanLine.query.filter_by(
            warehouse_id=warehouse_id, nomenclature_id=item.nomenclature_id
        ).first()
        if plan_line:
            plan_line.fulfilled_qty += item.qty


def _committed_by_warehouse_and_item():
    """{(warehouse_id, nomenclature_id): кол-во}, уже "закрытое" другими
    коробами, которые едут на этот склад, но еще не отмечены "Принято на
    складе" — черновики перемещения (короб отсканирован, но еще не уехал)
    и уже отправленные, но не принятые (см. movement.receive). После
    приемки это количество уже учтено в fulfilled_qty самой строки плана —
    поэтому принятые сюда не попадают, иначе вычлось бы дважды. Без этого
    remaining_qty продолжал бы показывать полную потребность склада, даже
    если она уже полностью закрыта едущими туда коробами, и подсказка
    маршрутизации короба слала бы туда больше, чем реально нужно."""
    rows = (
        db.session.query(
            MovementDocument.to_warehouse_id,
            BoxItem.nomenclature_id,
            func.sum(BoxItem.qty),
        )
        .join(MovementLine, MovementLine.document_id == MovementDocument.id)
        .join(BoxItem, BoxItem.box_id == MovementLine.box_id)
        .filter(
            or_(
                MovementDocument.status == "draft",
                and_(MovementDocument.status == "completed", MovementDocument.received_at.is_(None)),
            )
        )
        .group_by(MovementDocument.to_warehouse_id, BoxItem.nomenclature_id)
        .all()
    )
    return {(wh_id, nom_id): qty or 0 for wh_id, nom_id, qty in rows}


def _compute_routing(box):
    """Для содержимого короба ищет склады-города, где по актуальному плану
    отгрузок есть невыполненная потребность (remaining_qty > 0, за вычетом
    уже едущих туда коробов) хотя бы по одному товару из короба —
    группирует по складу и сортирует по тому, сколько из содержимого
    короба реально покрывает эту потребность (matched_qty), по убыванию.
    Пустой список — значит короб никому не нужен по текущему плану (короб
    можно пропустить)."""
    qty_by_item = {}
    for box_item in box.items:
        qty_by_item[box_item.nomenclature_id] = (
            qty_by_item.get(box_item.nomenclature_id, 0) + box_item.qty
        )
    if not qty_by_item:
        return []

    lines = ShipmentPlanLine.query.filter(
        ShipmentPlanLine.nomenclature_id.in_(qty_by_item.keys())
    ).all()
    committed = _committed_by_warehouse_and_item()

    by_warehouse = {}
    for line in lines:
        already_committed = committed.get((line.warehouse_id, line.nomenclature_id), 0)
        remaining = max(line.remaining_qty() - already_committed, 0)
        box_qty = qty_by_item.get(line.nomenclature_id, 0)
        if remaining <= 0 or box_qty <= 0:
            continue
        entry = by_warehouse.setdefault(
            line.warehouse_id,
            {"warehouse": line.warehouse, "matched_qty": 0.0, "total_remaining": 0.0, "items": []},
        )
        entry["matched_qty"] += min(remaining, box_qty)
        entry["total_remaining"] += remaining
        entry["items"].append({"nomenclature": line.nomenclature, "box_qty": box_qty, "remaining": remaining})

    return sorted(by_warehouse.values(), key=lambda e: -e["matched_qty"])


@bp.route("/")
def list_documents():
    documents = MovementDocument.query.order_by(MovementDocument.created_at.desc()).all()
    return render_template(
        "movement/list.html",
        documents=documents,
        route_box_number="",
        route_box=None,
        route_not_found=False,
        routing=[],
    )


@bp.route("/route-box")
def route_box():
    """Сканируем короб — показываем, на какой склад-город его нужно
    отправить по текущему плану отгрузок (или что потребности нет вообще
    ни у кого, и короб можно пропустить). Только просмотр — сам короб
    добавляется в конкретное перемещение отдельным действием ниже."""
    box_number = request.args.get("box_number", "").strip()
    documents = MovementDocument.query.order_by(MovementDocument.created_at.desc()).all()

    box = None
    routing = []
    not_found = False
    if box_number:
        box = Box.find_by_scanned_code(box_number)
        if not box:
            not_found = True
        else:
            routing = _compute_routing(box)

    return render_template(
        "movement/list.html",
        documents=documents,
        route_box_number=box_number,
        route_box=box,
        route_not_found=not_found,
        routing=routing,
    )


def _create_movement_line(doc, box):
    line = MovementLine(
        document_id=doc.id,
        box_id=box.id,
        from_warehouse_id=box.warehouse_id,
        from_cell_id=box.cell_id,
    )
    db.session.add(line)
    return line


def _find_conflicting_movement_line(box, exclude_doc_id=None):
    """Ищет строку с этим же коробом в ДРУГОМ черновике перемещения. Пока
    перемещение не завершено (status="draft"), box.warehouse_id еще не
    меняется — он меняется только в complete() — поэтому проверка
    "короб не на складе-отправителе" никак не ловит короб, отсканированный
    сразу в два разных черновика. Без этой отдельной проверки один и тот же
    короб мог молча "уехать" сразу в два перемещения одновременно."""
    query = MovementLine.query.join(
        MovementDocument, MovementLine.document_id == MovementDocument.id
    ).filter(MovementLine.box_id == box.id, MovementDocument.status == "draft")
    if exclude_doc_id is not None:
        query = query.filter(MovementDocument.id != exclude_doc_id)
    return query.first()


@bp.route("/route-box/add", methods=["POST"])
def route_box_add():
    """Быстрое добавление короба (найденного через route_box) в перемещение
    на рекомендованный склад — без ручного выбора документа: находит
    подходящий черновик (тот же склад-отправитель и склад назначения) или
    создает новый."""
    box_id = request.form.get("box_id", type=int)
    to_warehouse_id = request.form.get("to_warehouse_id", type=int)
    box = Box.query.get_or_404(box_id)
    to_warehouse = Warehouse.query.get_or_404(to_warehouse_id)

    doc = (
        MovementDocument.query.filter_by(
            from_warehouse_id=box.warehouse_id, to_warehouse_id=to_warehouse_id, status="draft"
        )
        .order_by(MovementDocument.created_at.desc())
        .first()
    )
    if not doc:
        doc = MovementDocument(
            number=next_number("movement"),
            from_warehouse_id=box.warehouse_id,
            to_warehouse_id=to_warehouse_id,
            created_by_id=current_user.id,
        )
        db.session.add(doc)
        db.session.flush()

    if doc.lines.filter_by(box_id=box.id).first():
        flash(f"Короб {box.box_number} уже в списке перемещения {doc.number}", "warning")
        return redirect(url_for("movement.list_documents"))

    conflict = _find_conflicting_movement_line(box, exclude_doc_id=doc.id)
    if conflict:
        flash(
            f"Короб {box.box_number} уже отсканирован в другое перемещение "
            f"{conflict.document.number} (черновик) — сначала уберите его оттуда.",
            "danger",
        )
        return redirect(url_for("movement.list_documents"))

    _create_movement_line(doc, box)
    db.session.commit()
    # Не уводим в сам документ перемещения — сборщик сканирует короба один
    # за другим на этой же странице; открыть документ можно из списка ниже,
    # когда сборка закончена.
    flash(f"Короб {box.box_number} добавлен в перемещение {doc.number} на «{to_warehouse.name}»", "success")
    return redirect(url_for("movement.list_documents"))


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


def _revert_shipment_fulfillment(box, warehouse_id):
    """Обратное действие к _apply_shipment_fulfillment — используется, когда
    администратор убирает короб из уже принятого (received_at заполнен)
    перемещения, чтобы не оставить задвоенное выполнение плана отгрузок."""
    for item in box.items:
        plan_line = ShipmentPlanLine.query.filter_by(
            warehouse_id=warehouse_id, nomenclature_id=item.nomenclature_id
        ).first()
        if plan_line:
            plan_line.fulfilled_qty = max(plan_line.fulfilled_qty - item.qty, 0)


@bp.route("/<int:doc_id>/boxes/add", methods=["POST"])
def add_box(doc_id):
    doc = MovementDocument.query.get_or_404(doc_id)
    editing_after_completion = doc.status != "draft"
    if editing_after_completion and not current_user.is_admin:
        flash("Документ уже завершен — изменить может только администратор", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    box_number = request.form.get("box_number", "").strip()
    box = Box.find_by_scanned_code(box_number)
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

    conflict = _find_conflicting_movement_line(box, exclude_doc_id=doc.id)
    if conflict:
        flash(
            f"Короб {box.box_number} уже отсканирован в другое перемещение "
            f"{conflict.document.number} (черновик) — сначала уберите его оттуда.",
            "danger",
        )
        return redirect(url_for("movement.detail", doc_id=doc.id))

    _create_movement_line(doc, box)

    if editing_after_completion:
        # Документ уже завершен (и, возможно, принят) — короб добавляется
        # админом задним числом, поэтому сразу переносим его так же, как
        # это сделал бы complete(), а не оставляем висеть "как будто в
        # черновике", где его никто больше не завершит.
        box.warehouse_id = doc.to_warehouse_id
        box.cell_id = None
        box.status = "open"
        if doc.received_at is not None:
            _apply_shipment_fulfillment(box, doc.to_warehouse_id)

    db.session.commit()
    if box.items.count() == 0:
        # Не блокируем — короб мог осознанно перемещаться пустым (например,
        # для повторного использования на другом складе), просто
        # предупреждаем, чтобы не увезти короб по ошибке вместо того, что
        # реально нужно было переместить.
        flash(f"Короб {box.box_number} добавлен в список перемещения, но он пустой — в нем нет товара.", "warning")
    else:
        flash(f"Короб {box.box_number} добавлен в список перемещения", "success")
    return redirect(url_for("movement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/lines/<int:line_id>/delete", methods=["POST"])
def delete_line(doc_id, line_id):
    doc = MovementDocument.query.get_or_404(doc_id)
    editing_after_completion = doc.status != "draft"
    if editing_after_completion and not current_user.is_admin:
        flash("Документ уже завершен — изменить может только администратор", "danger")
        return redirect(url_for("movement.detail", doc_id=doc_id))

    line = MovementLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()

    if editing_after_completion:
        # Отменяем эффект complete()/receive() именно для этого короба —
        # возвращаем его туда, где он был до перемещения, и снимаем
        # выполнение плана отгрузок, если оно уже было засчитано.
        box = line.box
        if doc.received_at is not None:
            _revert_shipment_fulfillment(box, doc.to_warehouse_id)
        box.warehouse_id = line.from_warehouse_id
        box.cell_id = line.from_cell_id
        box.status = "stored" if line.from_cell_id else "open"

    db.session.delete(line)
    db.session.commit()
    return redirect(url_for("movement.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/lines/<int:line_id>/set-cell", methods=["POST"])
def set_cell(doc_id, line_id):
    doc = MovementDocument.query.get_or_404(doc_id)
    editing_after_completion = doc.status != "draft"
    if editing_after_completion and not current_user.is_admin:
        flash("Документ уже завершен — изменить может только администратор", "danger")
        return redirect(url_for("movement.detail", doc_id=doc_id))

    line = MovementLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()

    cell_code = request.form.get("cell_code", "").strip()
    if not cell_code:
        line.to_cell_id = None
        if editing_after_completion:
            line.box.cell_id = None
            line.box.status = "open"
        db.session.commit()
        return redirect(url_for("movement.detail", doc_id=doc_id))

    cell = Cell.query.filter_by(warehouse_id=doc.to_warehouse_id, code=cell_code).first()
    if not cell:
        flash(f"Ячейка '{cell_code}' не найдена на складе «{doc.to_warehouse.name}»", "danger")
        return redirect(url_for("movement.detail", doc_id=doc_id))

    # Учитываем и уже стоящие в ячейке короба, и короба из других строк
    # этого же документа, уже нацеленные на эту ячейку — иначе на бумаге
    # ячейку легко "переполнить" еще до завершения перемещения.
    already_targeted = doc.lines.filter(
        MovementLine.to_cell_id == cell.id, MovementLine.id != line.id
    ).count()
    if cell.free_space(exclude_box_id=line.box_id) - already_targeted <= 0:
        flash(f"Ячейка '{cell_code}' заполнена (вмещает {CELL_CAPACITY} коробов)", "danger")
        return redirect(url_for("movement.detail", doc_id=doc_id))

    line.to_cell_id = cell.id
    if editing_after_completion:
        # Короб уже физически "приехал" — целевая ячейка меняется у самого
        # короба сразу, а не только у строки документа (иначе complete()
        # для этой строки больше не вызовется, и короб останется в старой
        # ячейке несмотря на изменение).
        line.box.cell_id = cell.id
        line.box.status = "stored"
    db.session.commit()
    flash(f"Короб {line.box.box_number}: ячейка назначения — {cell.code}", "success")
    return redirect(url_for("movement.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/delete", methods=["POST"])
def delete_document(doc_id):
    if not current_user.is_admin:
        flash("Удалять документы может только администратор", "danger")
        return redirect(url_for("movement.detail", doc_id=doc_id))

    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Можно удалить только черновик — завершенный документ уже переместил короба", "danger")
        return redirect(url_for("movement.detail", doc_id=doc_id))

    number = doc.number
    db.session.delete(doc)
    db.session.commit()
    flash(f"Список перемещения {number} удален", "success")
    return redirect(url_for("movement.list_documents"))


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
        # Выполнение плана отгрузок засчитывается не здесь, а отдельным
        # действием "Принято на складе" (см. receive()) — короб физически
        # мог еще ехать/лежать непроверенным на складе назначения.

    doc.status = "completed"
    doc.completed_at = datetime.utcnow()
    db.session.commit()
    flash(f"Перемещение {doc.number} завершено — {doc.lines.count()} короб(ов)", "success")
    return redirect(url_for("movement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/receive", methods=["POST"])
def receive(doc_id):
    """Подтверждение фактической приемки на складе назначения — только
    после этого выполнение зачисляется в план отгрузок (до этого товар
    висит в статусе "в пути", см. shipment_plan.dashboard)."""
    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.status != "completed":
        flash("Сначала завершите перемещение", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    if doc.received_at is not None:
        flash("Перемещение уже отмечено как принятое", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    for line in doc.lines:
        _apply_shipment_fulfillment(line.box, doc.to_warehouse_id)

    doc.received_at = datetime.utcnow()
    db.session.commit()
    flash(f"Перемещение {doc.number} принято на складе «{doc.to_warehouse.name}»", "success")
    return redirect(url_for("movement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/toggle-accounting", methods=["POST"])
def toggle_accounting(doc_id):
    """Ручная отметка бухгалтера "внесено в 1С" — просто галочка для
    контроля, никак не влияет на сам документ и не связана с автоматической
    выгрузкой (см. MovementDocument.accounting_entered_at)."""
    doc = MovementDocument.query.get_or_404(doc_id)
    doc.accounting_entered_at = None if doc.accounting_entered_at else datetime.utcnow()
    db.session.commit()
    return redirect(url_for("movement.list_documents"))


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


@bp.route("/waybills.pdf")
def export_waybills():
    """Печать накладных по выбранным в списке перемещениям (флажки в
    таблице) — по одной накладной на документ: номер и дата перемещения в
    шапке, штрихкод/наименование/количество (по всем коробам документа
    вместе) в табличной части."""
    doc_ids = request.args.getlist("doc_ids", type=int)
    if not doc_ids:
        flash("Выберите хотя бы одно перемещение для печати накладной", "danger")
        return redirect(url_for("movement.list_documents"))

    documents = (
        MovementDocument.query.filter(MovementDocument.id.in_(doc_ids))
        .order_by(MovementDocument.created_at.desc())
        .all()
    )
    if not documents:
        flash("Перемещения не найдены", "danger")
        return redirect(url_for("movement.list_documents"))

    data = build_movement_waybills_pdf(documents)
    fname = f"waybills_{timestamp_for_filename()}.pdf"
    return Response(
        data,
        mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition(fname, "inline")},
    )
