from datetime import datetime

from flask import Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import and_, func, or_

from ..extensions import db
from ..models import (
    AppSetting,
    Box,
    BoxItem,
    Cell,
    CELL_CAPACITY,
    MovementDocument,
    MovementLine,
    MovementReceiptDiscrepancy,
    Nomenclature,
    ShipmentPlanLine,
    Warehouse,
    UnplacedStock,
)
from ..utils.excel_io import export_movement_summary_to_excel, export_movement_to_excel, timestamp_for_filename
from ..utils.http import content_disposition
from ..utils.numbering import next_number
from ..utils.shipping_label_pdf import build_movement_shipping_labels_pdf
from ..utils.timezone import to_moscow
from ..utils.waybill_pdf import build_movement_waybills_pdf

bp = Blueprint("movement", __name__)
MOVEMENTS_PAGE_SIZE = 50


def _can_view_movement_document(doc):
    """Только ПРОСМОТР (список/детали/поиск короба) — не изменение. Пока
    документ "черновик" — его собирают сообща (см. route_box_add: черновик
    на маршрут теперь ищется без учета автора, чтобы разные сотрудники,
    сканирующие короба на одно направление, попадали в один документ),
    поэтому видеть и находить его должен любой, а не только автор — иначе
    для того, кто добавил короб не первым, документ выглядит так, будто
    короб "потерялся" (не находится через "Найти короб", не виден в своем
    списке "Перемещение"), хотя на самом деле он там и корректно учтен в
    остатке потребности. После завершения документ снова приватен (виден
    автору/админу/с movement_view_allowed) — сборка уже закончена, и учет
    "чей документ" снова важен. Изменять чужой документ это НЕ разрешает —
    см. _restrict_document_access: право "просмотр всех перемещений" тоже
    только для чтения, оно намеренно не участвует в проверке для
    изменяющих маршрутов."""
    return (
        current_user.is_admin
        or current_user.can_view_movements()
        or current_user.can_complete_movements()
        or current_user.can_receive_movements()
        or doc.created_by_id == current_user.id
        or doc.status in ("draft", "collected")
    )


# Отметки "внесено в 1С"/"заявка на МП создана"/номер заявки — не меняют
# сам документ перемещения (см. toggle_accounting/toggle_marketplace_request/
# update_marketplace_request_number), это просто пометки для контроля,
# которые может ставить любой, кто видит документ (например, бухгалтер или
# менеджер маркетплейса — не обязательно автор перемещения и не обязательно
# админ). Без этого исключения не-админ, открывший чужое перемещение через
# "Просмотр всех перемещений", получал 404 при попытке поставить галочку.
BOOKKEEPING_ENDPOINTS = {
    "movement.toggle_accounting",
    "movement.toggle_marketplace_request",
    "movement.mark_marketplace_request",
    "movement.update_marketplace_request_number",
}

# "Завершить перемещение"/"Принято на складе" — настоящее изменение
# документа (в отличие от BOOKKEEPING_ENDPOINTS выше), но право на него
# можно выдать отдельно от авторства/админства (см.
# User.movement_complete_allowed, настраивается в «Настройки» → доступ к
# разделам) — например, заведующему складом назначения, который принимает
# чужие перемещения.
COMPLETION_ENDPOINTS = {"movement.complete", "movement.mark_shipped"}
RECEIVING_ENDPOINTS = {"movement.receive"}
# Отметка "Транспорт забрал" прямо со страницы "Ждут транспорта" — в
# отличие от обычного mark_shipped (редиректит на детальную страницу
# документа, к которой у роли "логист" нет доступа), возвращает на тот же
# список (см. transport_mark_shipped). Логист может отмечать любой
# документ из своего списка, не только свой собственный.
LOGIST_ENDPOINTS = {"movement.transport_mark_shipped"}


@bp.before_request
def _restrict_document_access():
    document_id = (request.view_args or {}).get("doc_id")
    if document_id is None:
        return None
    doc = MovementDocument.query.get_or_404(document_id)
    readonly_endpoints = {"movement.detail", "movement.export_document"}
    if request.endpoint in readonly_endpoints or request.endpoint in BOOKKEEPING_ENDPOINTS:
        if not _can_view_movement_document(doc):
            abort(404)
        return None
    if request.endpoint in LOGIST_ENDPOINTS:
        if not (
            current_user.is_admin
            or current_user.is_logist_only()
            or current_user.can_complete_movements()
            or doc.created_by_id == current_user.id
        ):
            abort(404)
        return None
    if request.endpoint in COMPLETION_ENDPOINTS:
        if not (
            current_user.is_admin
            or current_user.can_complete_movements()
            or doc.created_by_id == current_user.id
        ):
            abort(404)
        return None
    if request.endpoint in RECEIVING_ENDPOINTS:
        if not current_user.can_receive_movements():
            abort(404)
        return None
    # Остальные изменяющие маршруты (добавить/убрать короб, удалить и
    # т.п.) — только автор или админ, как и раньше. "Просмотр всех
    # перемещений" здесь не действует (он read-only), а совместный доступ к
    # черновику дальше даем только через route_box_add (у него нет doc_id в
    # URL, этот хук на него не срабатывает) — не через произвольное
    # изменение чужого документа по прямой ссылке.
    if not (current_user.is_admin or doc.created_by_id == current_user.id):
        abort(404)
    return None


def _visible_movement_query():
    if current_user.can_view_movements() or current_user.can_receive_movements():
        return MovementDocument.query
    return MovementDocument.query.filter(
        or_(MovementDocument.created_by_id == current_user.id, MovementDocument.status.in_(("draft", "collected")))
    )


def _movement_pagination():
    """Последние перемещения постранично, по 50 документов."""
    page = max(request.args.get("page", 1, type=int), 1)
    return (
        _visible_movement_query()
        .order_by(MovementDocument.created_at.desc(), MovementDocument.id.desc())
        .paginate(page=page, per_page=MOVEMENTS_PAGE_SIZE, error_out=False)
    )

SHIPPING_LABEL_SENDER_KEY = "movement_shipping_label_sender"


def get_shipping_label_sender_override():
    """Если задано — печатается на стикерах отправления как "Отправитель"
    вместо названия конкретного склада-отправителя документа, сразу для
    всех направлений (не нужно менять по одному на каждый склад)."""
    setting = AppSetting.query.get(SHIPPING_LABEL_SENDER_KEY)
    return setting.value if setting and setting.value else None


def _plan_line_start(line):
    return line.period_start or (line.plan.period_start if line.plan else None)


def _shipment_is_in_plan(line, shipped_at):
    period_start = _plan_line_start(line)
    return not period_start or not shipped_at or shipped_at.date() >= period_start


def _apply_shipment_fulfillment(box, warehouse_id, shipped_at=None):
    """Если короб приехал на склад-город из плана отгрузок (см.
    shipment_plan) — дописывает выполнение плана по товарам в этом коробе.
    Для обычных складов (не из плана) не находит ни одной строки — no-op."""
    for item in box.items:
        plan_line = ShipmentPlanLine.query.filter_by(
            warehouse_id=warehouse_id, nomenclature_id=item.nomenclature_id
        ).first()
        if plan_line and _shipment_is_in_plan(plan_line, shipped_at):
            plan_line.fulfilled_qty += item.qty


def _committed_by_warehouse_and_item(period_start=None):
    """{(warehouse_id, nomenclature_id): кол-во}, уже "закрытое" другими
    коробами, которые едут на этот склад, но еще не отмечены "Принято на
    складе" — черновики перемещения (короб отсканирован, но еще не уехал)
    и уже отправленные, но не принятые (см. movement.receive). После
    приемки это количество уже учтено в fulfilled_qty самой строки плана —
    поэтому принятые сюда не попадают, иначе вычлось бы дважды. Без этого
    remaining_qty продолжал бы показывать полную потребность склада, даже
    если она уже полностью закрыта едущими туда коробами, и подсказка
    маршрутизации короба слала бы туда больше, чем реально нужно."""
    query = (
        db.session.query(
            MovementDocument.to_warehouse_id,
            BoxItem.nomenclature_id,
            func.sum(BoxItem.qty),
        )
        .join(MovementLine, MovementLine.document_id == MovementDocument.id)
        .join(BoxItem, BoxItem.box_id == MovementLine.box_id)
        .filter(
            or_(
                MovementDocument.status.in_(("draft", "collected")),
                and_(MovementDocument.status == "completed", MovementDocument.received_at.is_(None)),
            )
        )
    )
    if period_start:
        window_start = datetime.combine(period_start, datetime.min.time())
        query = query.filter(
            or_(
                and_(
                    MovementDocument.status.in_(("draft", "collected")),
                    MovementLine.scanned_at >= window_start,
                ),
                and_(
                    MovementDocument.status == "completed",
                    func.coalesce(
                        MovementDocument.completed_at,
                        MovementLine.scanned_at,
                    ) >= window_start,
                ),
            )
        )
    rows = query.group_by(
        MovementDocument.to_warehouse_id, BoxItem.nomenclature_id
    ).all()
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
    committed_by_period = {}

    by_warehouse = {}
    for line in lines:
        period_start = _plan_line_start(line)
        if period_start not in committed_by_period:
            committed_by_period[period_start] = _committed_by_warehouse_and_item(period_start)
        committed = committed_by_period[period_start]
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


def _transport_waiting_query():
    """Перемещения, ожидающие передачи транспорту — сборка завершена,
    заявка на МП подана, но "Транспорт забрал" еще не отмечено (см.
    movement.mark_shipped/toggle_shipped). Тот же критерий, что и у бакета
    "transport" в management.py (панель руководителя)."""
    return MovementDocument.query.filter(
        MovementDocument.status == "completed",
        MovementDocument.marketplace_request_created_at.isnot(None),
        MovementDocument.shipped_at.is_(None),
    )


@bp.route("/transport")
def transport_list():
    """Единственная страница, доступная роли "логист" (см. чат — видит
    только перемещения со статусом "ждет транспорта"), но открыта и
    остальным, кто уже видит перемещения — не только этой роли."""
    documents = _transport_waiting_query().order_by(
        MovementDocument.marketplace_request_created_at.asc()
    ).all()
    return render_template("movement/transport.html", documents=documents)


@bp.route("/transport/export-summary.xlsx")
def transport_export_summary():
    """Сводная по перемещениям, ожидающим транспорт — тот же формат, что и
    общий export_summary, но только по этой выборке (см. чат — "логист
    может выгружать по ним сводную")."""
    documents = _transport_waiting_query().order_by(
        MovementDocument.marketplace_request_created_at.asc()
    ).all()
    data = export_movement_summary_to_excel(documents)
    fname = f"transport_summary_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


@bp.route("/<int:doc_id>/transport/mark-shipped", methods=["POST"])
def transport_mark_shipped(doc_id):
    """Тот же mark_shipped, но со страницы "Ждут транспорта" — возвращает
    туда же, а не на детальную страницу документа (к которой у роли
    "логист" нет доступа, см. чат)."""
    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.status != "completed":
        flash("Сначала завершите сборку перемещения", "danger")
    elif not doc.marketplace_request_created_at or not (
        doc.marketplace_request_number or ""
    ).strip():
        flash("Сначала внесите номер и отметьте подачу заявки на МП", "danger")
    elif doc.shipped_at is not None:
        flash("Передача транспорту уже зафиксирована", "warning")
    else:
        doc.shipped_at = datetime.utcnow()
        db.session.commit()
        flash(f"Перемещение {doc.number} передано транспорту", "success")
    return redirect(url_for("movement.transport_list"))


@bp.route("/")
def list_documents():
    pagination = _movement_pagination()
    return render_template(
        "movement/list.html",
        documents=pagination.items,
        pagination=pagination,
        route_box_number="",
        route_box=None,
        route_not_found=False,
        routing=[],
    )


@bp.route("/merge", methods=["POST"])
def merge_documents():
    """Свести несколько параллельных черновиков на один и тот же маршрут
    (например, несколько сотрудников собирали одно направление порознь и в
    итоге получили разные документы вместо одного) в один итоговый —
    короба (MovementLine) переезжают в новый документ без задвоения, старые
    помечаются "merged" и остаются в истории — ничего не удаляется. Только
    для админа: объединение задним числом может незаметно перемешать
    короба, собранные разными людьми, это должен делать кто-то, кто видит
    всю картину."""
    if not current_user.is_admin:
        flash("Объединять перемещения может только администратор", "danger")
        return redirect(url_for("movement.list_documents"))

    doc_ids = request.form.getlist("doc_ids", type=int)
    if len(doc_ids) < 2:
        flash("Выберите минимум два документа для объединения", "danger")
        return redirect(url_for("movement.list_documents"))

    docs = MovementDocument.query.filter(MovementDocument.id.in_(doc_ids)).all()
    if len(docs) != len(set(doc_ids)):
        flash("Не удалось найти все выбранные документы", "danger")
        return redirect(url_for("movement.list_documents"))
    if any(d.status != "draft" for d in docs):
        flash("Объединять можно только черновики (не завершенные и не уже объединенные документы)", "danger")
        return redirect(url_for("movement.list_documents"))
    from_ids = {d.from_warehouse_id for d in docs}
    to_ids = {d.to_warehouse_id for d in docs}
    if len(from_ids) > 1 or len(to_ids) > 1:
        flash(
            "Выбранные документы ведут на разные маршруты — объединять можно только "
            "документы с одинаковым складом-отправителем и складом назначения",
            "danger",
        )
        return redirect(url_for("movement.list_documents"))

    merged = MovementDocument(
        number=next_number("movement"),
        from_warehouse_id=from_ids.pop(),
        to_warehouse_id=to_ids.pop(),
        created_by_id=current_user.id,
    )
    db.session.add(merged)
    db.session.flush()

    seen_box_ids = set()
    duplicate_box_numbers = []
    moved_count = 0
    for doc in docs:
        for line in doc.lines.all():
            if line.box_id in seen_box_ids:
                duplicate_box_numbers.append(line.box.box_number)
                db.session.delete(line)
                continue
            seen_box_ids.add(line.box_id)
            line.document_id = merged.id
            moved_count += 1
        doc.status = "merged"
        doc.merged_into_id = merged.id

    db.session.commit()

    message = f"Документы объединены в {merged.number}: {moved_count} короб(ов) из {len(docs)} документов"
    if duplicate_box_numbers:
        flash(
            message + f". Внимание: короб(а) {', '.join(duplicate_box_numbers)} "
            "были в нескольких документах — учтены один раз",
            "warning",
        )
    else:
        flash(message, "success")
    return redirect(url_for("movement.detail", doc_id=merged.id))


@bp.route("/route-box")
def route_box():
    """Сканируем короб — показываем, на какой склад-город его нужно
    отправить по текущему плану отгрузок (или что потребности нет вообще
    ни у кого, и короб можно пропустить). Только просмотр — сам короб
    добавляется в конкретное перемещение отдельным действием ниже."""
    box_number = request.args.get("box_number", "").strip()
    pagination = _movement_pagination()

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
        documents=pagination.items,
        pagination=pagination,
        route_box_number=box_number,
        route_box=box,
        route_not_found=not_found,
        routing=routing,
    )


@bp.route("/find-box")
def find_box():
    """Поиск короба по всем перемещениям — где он числится (в т.ч. пока
    еще черновиком). Нужно в первую очередь тогда, когда добавить короб в
    новое перемещение не дает блокировка "уже в другом перемещении" (см.
    _find_conflicting_movement_line) — здесь видно, в каком именно."""
    box_number = request.args.get("box_number", "").strip()
    pagination = _movement_pagination()

    box = None
    not_found = False
    lines = []
    if box_number:
        box = Box.find_by_scanned_code(box_number)
        if not box:
            not_found = True
        else:
            lines_query = MovementLine.query.filter_by(box_id=box.id).join(
                MovementDocument, MovementLine.document_id == MovementDocument.id
            )
            if not current_user.can_view_movements():
                # Черновик собирают сообща (см. _can_view_movement_document) —
                # без этого условия короб, добавленный в чужой черновик через
                # "Куда везти короб", выглядел бы "не найденным ни в одном
                # перемещении" для того, кто его туда положил.
                lines_query = lines_query.filter(
                    or_(MovementDocument.created_by_id == current_user.id, MovementDocument.status.in_(("draft", "collected")))
                )
            lines = lines_query.order_by(MovementDocument.created_at.desc()).all()

    return render_template(
        "movement/list.html",
        documents=pagination.items,
        pagination=pagination,
        route_box_number="",
        route_box=None,
        route_not_found=False,
        routing=[],
        find_box_number=box_number,
        find_box=box,
        find_box_not_found=not_found,
        find_box_lines=lines,
    )


def _create_movement_line(doc, box):
    line = MovementLine(
        document_id=doc.id,
        box_id=box.id,
        from_warehouse_id=box.warehouse_id,
        from_cell_id=box.cell_id,
    )
    db.session.add(line)
    box.mark_scanned(current_user)
    return line


def _find_conflicting_movement_line(box, exclude_doc_id=None):
    """Ищет строку с этим же коробом в ДРУГОМ еще не принятом перемещении —
    черновике или уже завершенном, но "в пути" (received_at не заполнен).
    box.warehouse_id обновляется на склад назначения уже в complete(), а не
    в receive() (см. комментарий там) — поэтому короб, отправленный, но еще
    не принятый на месте, выглядит для проверки "короб не на складе-
    отправителе" как будто он уже там, и эта проверка не ловит его
    повторное добавление в другое перемещение (например, через "Куда везти
    короб" — там склад-отправитель нового документа берется из ТЕКУЩЕГО
    box.warehouse_id, то есть автоматически совпадет). Без этой отдельной
    проверки один и тот же физический короб мог молча "уехать" сразу в два
    разных перемещения одновременно."""
    query = MovementLine.query.join(
        MovementDocument, MovementLine.document_id == MovementDocument.id
    ).filter(
        MovementLine.box_id == box.id,
        or_(
            MovementDocument.status.in_(("draft", "collected")),
            and_(MovementDocument.status == "completed", MovementDocument.received_at.is_(None)),
        ),
    )
    if exclude_doc_id is not None:
        query = query.filter(MovementDocument.id != exclude_doc_id)
    return query.first()


def _conflict_status_label(document):
    if document.status == "draft":
        return "черновик"
    if document.status == "collected":
        return "собрано"
    return "в пути, еще не принят"


def _conflict_message(box, conflict):
    if current_user.is_admin or conflict.document.created_by_id == current_user.id:
        return (
            f"Короб {box.box_number} уже отсканирован в другое перемещение "
            f"{conflict.document.number} ({_conflict_status_label(conflict.document)}) — "
            "сначала уберите его оттуда."
        )
    return f"Короб {box.box_number} уже используется в другом активном перемещении."


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

    if current_user.warehouse_id and box.warehouse_id != current_user.warehouse_id:
        flash(
            f"Короб {box.box_number} относится к складу «{box.warehouse.name}». "
            f"Ваш рабочий склад — «{current_user.warehouse.name}».",
            "danger",
        )
        return redirect(url_for("movement.list_documents"))

    # Ищем черновик на этот маршрут независимо от того, кто его начал —
    # иначе двое сотрудников, собирающих одно направление порознь, каждый
    # находили бы только СВОИ черновики (owned_query ограничивает чужие) и
    # получали бы разные документы вместо одного общего (см. также
    # new_document, где этот же поиск сделан без owned_query).
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
        flash(_conflict_message(box, conflict), "danger")
        return redirect(url_for("movement.list_documents"))

    _create_movement_line(doc, box)
    if to_warehouse.marketplace == "ozon" and doc.lines.count() >= 30:
        doc.status = "collected"
    db.session.commit()
    # Не уводим в сам документ перемещения — сборщик сканирует короба один
    # за другим на этой же странице; открыть документ можно из списка ниже,
    # когда сборка закончена.
    contents = ", ".join(
        f"{item.nomenclature.name}: {item.qty:g} {item.nomenclature.unit}"
        for item in box.items
    ) or "короб пуст"
    flash(
        f"Короб {box.box_number} добавлен в перемещение {doc.number} на «{to_warehouse.name}». "
        f"Содержимое: {contents}"
        + (" Перемещение достигло лимита 30 коробов и отмечено как собранное." if doc.status == "collected" else ""),
        "success",
    )
    return redirect(url_for("movement.list_documents"))


@bp.route("/box-transfer")
def box_transfer():
    """Отдельная операция перепаковки товара из одного короба в другой."""
    source_number = request.args.get("source_box_number", "").strip()
    source_box = Box.find_by_scanned_code(source_number) if source_number else None
    return render_template(
        "movement/box_transfer.html",
        source_box_number=source_number,
        source_box=source_box,
        source_not_found=bool(source_number and not source_box),
        items=source_box.items.all() if source_box else [],
    )


@bp.route("/box-transfer/items/<int:item_id>", methods=["POST"])
def transfer_box_item(item_id):
    source_box_id = request.form.get("source_box_id", type=int)
    box_item = BoxItem.query.filter_by(id=item_id, box_id=source_box_id).first_or_404()
    source_box = box_item.box
    target_number = request.form.get("target_box_number", "").strip()
    qty = request.form.get("qty", type=float)

    if not qty or qty <= 0 or qty > box_item.qty:
        flash(f"Укажите корректное количество (доступно {box_item.qty:g})", "danger")
        return redirect(url_for("movement.box_transfer", source_box_number=source_box.box_number))

    target_box = Box.find_by_scanned_code(target_number, warehouse_id=source_box.warehouse_id)
    if not target_box:
        flash(
            f"Короб '{target_number}' не найден на складе «{source_box.warehouse.name}»",
            "danger",
        )
        return redirect(url_for("movement.box_transfer", source_box_number=source_box.box_number))
    if target_box.id == source_box.id:
        flash("Целевой короб совпадает с исходным", "danger")
        return redirect(url_for("movement.box_transfer", source_box_number=source_box.box_number))

    item = box_item.nomenclature
    box_item.qty -= qty
    if box_item.qty <= 0:
        db.session.delete(box_item)

    target_item = BoxItem.query.filter_by(
        box_id=target_box.id, nomenclature_id=item.id
    ).first()
    if target_item:
        target_item.qty += qty
    else:
        db.session.add(BoxItem(box_id=target_box.id, nomenclature_id=item.id, qty=qty))

    source_box.mark_scanned(current_user)
    target_box.mark_scanned(current_user)
    db.session.commit()
    flash(
        f"Перенесено: {item.name} — {qty:g} {item.unit}, "
        f"{source_box.box_number} → {target_box.box_number}",
        "success",
    )
    return redirect(url_for("movement.box_transfer", source_box_number=source_box.box_number))


@bp.route("/new", methods=["GET", "POST"])
def new_document():
    if request.method == "GET":
        warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.code).all()
        return render_template("movement/new.html", warehouses=warehouses, assigned_warehouse=current_user.warehouse)

    # Администратор может выбрать отправителя вручную, даже если ему самому
    # назначен рабочий склад. Для обычного сотрудника поле всегда жестко
    # определяется его рабочим складом.
    from_warehouse_id = (
        request.form.get("from_warehouse_id", type=int)
        if current_user.is_admin
        else current_user.warehouse_id
    )
    if not current_user.is_admin and not from_warehouse_id:
        flash("Администратор еще не назначил вам рабочий склад", "danger")
        return redirect(url_for("movement.new_document"))
    to_warehouse_id = request.form.get("to_warehouse_id", type=int)
    if not from_warehouse_id or not to_warehouse_id:
        flash("Выберите склад-отправитель и склад назначения", "danger")
        return redirect(url_for("movement.new_document"))

    if from_warehouse_id == to_warehouse_id:
        flash("Склад-отправитель и склад назначения не могут совпадать", "danger")
        return redirect(url_for("movement.new_document"))

    # Если черновик на этот же маршрут (тот же склад-отправитель и склад
    # назначения) уже кто-то начал собирать — присоединяемся к нему вместо
    # создания дубля, точно так же, как это уже работает при добавлении
    # короба через "Куда везти короб" (см. route_box_add). Иначе двое
    # сотрудников, собирающих одно направление независимо друг от друга,
    # получали бы два отдельных документа вместо одного.
    doc = (
        MovementDocument.query.filter_by(
            from_warehouse_id=from_warehouse_id, to_warehouse_id=to_warehouse_id, status="draft"
        )
        .order_by(MovementDocument.created_at.desc())
        .first()
    )
    if doc:
        flash(f"На этот маршрут уже есть черновик {doc.number} — продолжайте собирать в него", "info")
        return redirect(url_for("movement.detail", doc_id=doc.id))

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


@bp.route("/<int:doc_id>/change-sender", methods=["POST"])
def change_sender(doc_id):
    """Исправление склада-отправителя администратором до отправки."""
    if not current_user.is_admin:
        abort(404)
    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.status not in ("draft", "collected"):
        flash("Склад-отправитель можно изменить только до отправки перемещения", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    warehouse_id = request.form.get("from_warehouse_id", type=int)
    warehouse = Warehouse.query.filter_by(id=warehouse_id, is_active=True).first()
    if not warehouse:
        flash("Выбранный склад не найден или отключен", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))
    if warehouse.id == doc.to_warehouse_id:
        flash("Склад-отправитель и склад назначения не могут совпадать", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    wrong_boxes = [line.box.box_number for line in doc.lines if line.box.warehouse_id != warehouse.id]
    if wrong_boxes:
        shown = ", ".join(wrong_boxes[:5]) + ("…" if len(wrong_boxes) > 5 else "")
        flash(
            f"Нельзя выбрать этот склад: короба {shown} находятся на другом складе. "
            "Сначала переместите их документом.",
            "danger",
        )
        return redirect(url_for("movement.detail", doc_id=doc.id))

    doc.from_warehouse_id = warehouse.id
    for line in doc.lines:
        line.from_warehouse_id = warehouse.id
        line.from_cell_id = line.box.cell_id
    db.session.commit()
    flash(f"Склад-отправитель изменен на «{warehouse.name}»", "success")
    return redirect(url_for("movement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>")
def detail(doc_id):
    doc = MovementDocument.query.get_or_404(doc_id)
    lines = doc.lines.order_by(MovementLine.id.asc()).all()
    warehouses = (
        Warehouse.query.filter_by(is_active=True).order_by(Warehouse.code).all()
        if current_user.is_admin and doc.status in ("draft", "collected")
        else []
    )
    return render_template("movement/detail.html", doc=doc, lines=lines, warehouses=warehouses)


def _revert_shipment_fulfillment(box, warehouse_id, shipped_at=None):
    """Обратное действие к _apply_shipment_fulfillment — используется, когда
    администратор убирает короб из уже принятого (received_at заполнен)
    перемещения, чтобы не оставить задвоенное выполнение плана отгрузок."""
    for item in box.items:
        plan_line = ShipmentPlanLine.query.filter_by(
            warehouse_id=warehouse_id, nomenclature_id=item.nomenclature_id
        ).first()
        if plan_line and _shipment_is_in_plan(plan_line, shipped_at):
            plan_line.fulfilled_qty = max(plan_line.fulfilled_qty - item.qty, 0)


def _revert_line_effects(doc, line):
    """Отменяет эффект complete()/receive() именно для этого короба —
    возвращает его туда, где он был до перемещения, и снимает выполнение
    плана отгрузок, если оно уже было засчитано. Используется и при
    удалении одной строки, и при удалении завершенного документа целиком
    (см. delete_line, delete_document)."""
    box = line.box
    if doc.received_at is not None:
        _revert_shipment_fulfillment(box, doc.to_warehouse_id, doc.shipped_at)
    box.warehouse_id = line.from_warehouse_id
    box.cell_id = line.from_cell_id
    box.status = "stored" if line.from_cell_id else "open"


def _movement_doc_for_box_correction(box_id):
    """Последний уже выгруженный в 1С документ перемещения, в котором сейчас
    числится этот короб (по последней строке на этот box_id) — если после
    выгрузки его состав правят (см. boxes.add_item/update_item/move_item/
    delete_item), именно ЭТОТ документ в 1С перестал соответствовать
    фактическому составу и должен быть скорректирован (см.
    MovementDocument.composition_changed_at,
    integration_1c._movement_corrections_export)."""
    line = (
        MovementLine.query.join(MovementDocument)
        .filter(MovementLine.box_id == box_id, MovementDocument.synced_to_1c_at.isnot(None))
        .order_by(MovementLine.id.desc())
        .first()
    )
    return line.document if line else None


def flag_movement_dirty_for_box(box_id):
    """Вызывается из boxes.py при правке состава короба — помечает
    перемещение, которым этот короб уже уехал и выгрузился в 1С (если такое
    есть), как требующее коррекции в 1С."""
    doc = _movement_doc_for_box_correction(box_id)
    if doc is not None:
        doc.composition_changed_at = datetime.utcnow()


@bp.route("/<int:doc_id>/boxes/add", methods=["POST"])
def add_box(doc_id):
    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.status == "collected":
        flash("В Ozon-перемещении уже 30 коробов. Следующий короб добавляйте в новый документ.", "warning")
        return redirect(url_for("movement.detail", doc_id=doc.id))
    editing_after_completion = doc.status not in ("draft", "collected")
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
        flash(_conflict_message(box, conflict), "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    _create_movement_line(doc, box)

    if doc.to_warehouse.marketplace == "ozon" and doc.lines.count() >= 30:
        doc.status = "collected"

    if doc.synced_to_1c_at is not None:
        # Документ уже выгружен в 1С — новый короб в нем 1С еще не видела,
        # значит документ там нужно дозаполнить (см. composition_changed_at).
        doc.composition_changed_at = datetime.utcnow()

    if editing_after_completion:
        # Документ уже завершен (и, возможно, принят) — короб добавляется
        # админом задним числом, поэтому сразу переносим его так же, как
        # это сделал бы complete(), а не оставляем висеть "как будто в
        # черновике", где его никто больше не завершит.
        box.warehouse_id = doc.to_warehouse_id
        box.cell_id = None
        box.status = "open"
        if doc.received_at is not None:
            _apply_shipment_fulfillment(box, doc.to_warehouse_id, doc.shipped_at)

    db.session.commit()
    if doc.status == "collected":
        flash(
            f"Короб {box.box_number} добавлен. В перемещении 30 коробов — оно отмечено как собранное.",
            "success",
        )
    elif box.items.count() == 0:
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
    editing_after_completion = doc.status not in ("draft", "collected")
    if editing_after_completion and not current_user.is_admin:
        flash("Документ уже завершен — изменить может только администратор", "danger")
        return redirect(url_for("movement.detail", doc_id=doc_id))

    line = MovementLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()

    if editing_after_completion:
        _revert_line_effects(doc, line)

    if doc.synced_to_1c_at is not None:
        # Документ уже выгружен в 1С с этим коробом в составе — теперь его
        # там нужно убрать (см. composition_changed_at).
        doc.composition_changed_at = datetime.utcnow()

    db.session.delete(line)
    if doc.status == "collected":
        doc.status = "draft"
    db.session.commit()
    return redirect(url_for("movement.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/split", methods=["POST"])
def split_document(doc_id):
    """Выносит выбранные короба в отдельный документ того же маршрута."""
    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.status not in ("draft", "collected"):
        flash("Дробить можно только перемещение до отправки", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))
    line_ids = request.form.getlist("line_ids", type=int)
    lines = doc.lines.filter(MovementLine.id.in_(line_ids)).all() if line_ids else []
    if not lines or len(lines) == doc.lines.count():
        flash("Выберите часть коробов: минимум один, но не все", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    new_doc = MovementDocument(
        number=next_number("movement"),
        from_warehouse_id=doc.from_warehouse_id,
        to_warehouse_id=doc.to_warehouse_id,
        created_by_id=current_user.id,
    )
    db.session.add(new_doc)
    db.session.flush()
    for line in lines:
        line.document_id = new_doc.id

    doc.status = "collected" if doc.to_warehouse.marketplace == "ozon" and doc.lines.count() >= 30 else "draft"
    new_doc.status = "collected" if doc.to_warehouse.marketplace == "ozon" and len(lines) >= 30 else "draft"
    db.session.commit()
    flash(f"Создано отдельное перемещение {new_doc.number}: {len(lines)} короб(ов)", "success")
    return redirect(url_for("movement.detail", doc_id=new_doc.id))


@bp.route("/<int:doc_id>/lines/<int:line_id>/set-cell", methods=["POST"])
def set_cell(doc_id, line_id):
    doc = MovementDocument.query.get_or_404(doc_id)
    editing_after_completion = doc.status not in ("draft", "collected")
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

    if doc.status == "completed":
        # Завершенный документ уже физически переместил короба — при
        # удалении возвращаем их туда, где они были до перемещения (та же
        # логика, что и при удалении отдельной строки завершенного
        # документа, см. delete_line), иначе короба останутся числиться
        # на складе назначения без какого-либо документа-основания.
        if doc.received_at is not None:
            _revert_document_receipt(doc)
            doc.received_at = None
        for line in doc.lines:
            _revert_line_effects(doc, line)

    for source in doc.merged_from:
        # Удаляем итоговый документ объединения — исходные документы не
        # должны остаться со ссылкой на несуществующий; их содержимое уже
        # уехало в удаляемый документ и вместе с ним пропадает, поэтому
        # возвращаем их в обычные (пустые) черновики, а не оставляем
        # висеть в статусе "merged" без цели.
        source.merged_into_id = None
        source.status = "draft"

    number = doc.number
    db.session.delete(doc)
    db.session.commit()
    flash(f"Список перемещения {number} удален", "success")
    return redirect(url_for("movement.list_documents"))


@bp.route("/<int:doc_id>/complete", methods=["POST"])
def complete(doc_id):
    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.status not in ("draft", "collected"):
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

    doc.sent_qty_snapshot = doc.total_item_qty()
    doc.status = "completed"
    doc.completed_at = datetime.utcnow()
    db.session.commit()
    flash(f"Перемещение {doc.number} завершено — {doc.lines.count()} короб(ов)", "success")
    return redirect(url_for("movement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/mark-shipped", methods=["POST"])
def mark_shipped(doc_id):
    """Фиксирует момент, когда транспорт физически забрал товар."""
    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.status != "completed":
        flash("Сначала завершите сборку перемещения", "danger")
    elif not doc.marketplace_request_created_at or not (
        doc.marketplace_request_number or ""
    ).strip():
        flash("Сначала внесите номер и отметьте подачу заявки на МП", "danger")
    elif doc.shipped_at is not None:
        flash("Передача транспорту уже зафиксирована", "warning")
    else:
        doc.shipped_at = datetime.utcnow()
        db.session.commit()
        flash(f"Перемещение {doc.number} передано транспорту", "success")
    return redirect(url_for("movement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/toggle-shipped", methods=["POST"])
def toggle_shipped(doc_id):
    """Быстрая администраторская отметка передачи груза транспорту.

    В отличие от обычной кнопки на странице документа, этот маршрут
    возвращает JSON и используется галочкой в общем списке перемещений.
    Снять ошибочную отметку можно только до фиксации приемки на МП.
    """
    if not current_user.is_admin:
        abort(403)

    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.status != "completed":
        return jsonify(ok=False, error="Сначала завершите сборку перемещения"), 400
    if not doc.marketplace_request_created_at or not (
        doc.marketplace_request_number or ""
    ).strip():
        return jsonify(
            ok=False,
            error="Сначала внесите номер и отметьте подачу заявки на МП",
        ), 400
    if doc.received_at is not None:
        return jsonify(
            ok=False,
            error="Нельзя изменить отметку: товар уже принят на маркетплейсе",
        ), 400

    doc.shipped_at = None if doc.shipped_at is not None else datetime.utcnow()
    db.session.commit()
    return jsonify(
        ok=True,
        checked=doc.shipped_at is not None,
        at=(to_moscow(doc.shipped_at).strftime("%d.%m.%Y %H:%M") if doc.shipped_at else ""),
    )


def _expected_qty_by_nomenclature(doc):
    """Сколько какого товара по факту едет в этом перемещении — сумма по
    всем коробам документа."""
    expected = {}
    for line in doc.lines:
        for item in line.box.items:
            expected[item.nomenclature_id] = expected.get(item.nomenclature_id, 0) + item.qty
    return expected


def _apply_receipt_stock_difference(doc, nomenclature_id, expected_qty, received_qty):
    """Приводит физический остаток склада назначения к факту приемки.

    Недовоз списывается из содержимого коробов этого перемещения, излишек
    попадает в неразмещенный остаток — его затем можно упаковать обычным
    размещением. Документ сохраняет исходное отправленное количество в
    sent_qty_snapshot.
    """
    shortage = max(expected_qty - received_qty, 0)
    for line in doc.lines.order_by(MovementLine.id.desc()).all():
        if shortage <= 0:
            break
        box_item = BoxItem.query.filter_by(
            box_id=line.box_id, nomenclature_id=nomenclature_id
        ).first()
        if not box_item:
            continue
        take = min(box_item.qty, shortage)
        box_item.qty -= take
        shortage -= take
        if box_item.qty <= 0:
            db.session.delete(box_item)

    excess = max(received_qty - expected_qty, 0)
    if excess:
        UnplacedStock.add(doc.to_warehouse_id, nomenclature_id, excess)


def _revert_document_receipt(doc):
    """Отменяет учет фактической приемки перед удалением документа."""
    current = _expected_qty_by_nomenclature(doc)
    discrepancies = {d.nomenclature_id: d for d in doc.discrepancies}
    for nomenclature_id, qty in current.items():
        actual_qty = discrepancies.get(nomenclature_id).received_qty if nomenclature_id in discrepancies else qty
        plan_line = ShipmentPlanLine.query.filter_by(
            warehouse_id=doc.to_warehouse_id, nomenclature_id=nomenclature_id
        ).first()
        if plan_line and _shipment_is_in_plan(plan_line, doc.shipped_at or doc.completed_at):
            plan_line.fulfilled_qty = max(plan_line.fulfilled_qty - actual_qty, 0)

    for discrepancy in doc.discrepancies:
        if discrepancy.shortage_qty():
            first_line = doc.lines.first()
            if first_line:
                item = BoxItem.query.filter_by(
                    box_id=first_line.box_id,
                    nomenclature_id=discrepancy.nomenclature_id,
                ).first()
                if item:
                    item.qty += discrepancy.shortage_qty()
                else:
                    db.session.add(BoxItem(
                        box_id=first_line.box_id,
                        nomenclature_id=discrepancy.nomenclature_id,
                        qty=discrepancy.shortage_qty(),
                    ))
        if discrepancy.excess_qty():
            UnplacedStock.consume(
                doc.to_warehouse_id,
                discrepancy.nomenclature_id,
                discrepancy.excess_qty(),
            )


@bp.route("/<int:doc_id>/receive", methods=["GET", "POST"])
def receive(doc_id):
    """Единственная кнопка "Принято на складе" (см. чат: раньше рядом была
    отдельная "Принято с расхождением" — убрали, эта форма покрывает оба
    случая сразу). По каждому товару показываем, сколько отправлено, по
    умолчанию проставлено столько же — обычная приемка без расхождений это
    просто подтверждает как есть; если по факту меньше/больше, исправляют
    нужные строки. Именно введенное здесь количество, а не то, что было
    упаковано в коробах, зачисляется в выполнение плана отгрузок; настоящее
    расхождение сохраняется отдельной строкой (см. MovementReceiptDiscrepancy)
    для учета, а не молча теряется. Статус "Отгружено" фиксируется раньше
    отдельной кнопкой "Транспорт забрал"; здесь подтверждается именно
    фактическая приемка маркетплейсом."""
    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.status != "completed":
        flash("Сначала завершите перемещение", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    if doc.received_at is not None:
        flash("Перемещение уже отмечено как принятое", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    if doc.marketplace_request_created_at is None or not (
        doc.marketplace_request_number or ""
    ).strip():
        flash(
            "Сначала внесите номер заявки на маркетплейс и отметьте, что заявка создана — "
            "оба шага обязательны перед приемкой на складе",
            "danger",
        )
        return redirect(url_for("movement.detail", doc_id=doc.id))

    if doc.shipped_at is None:
        flash("Сначала отметьте, что товар забрал транспорт", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    expected = _expected_qty_by_nomenclature(doc)
    if not expected:
        flash("В списке нет коробов с товаром", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    if request.method == "GET":
        nomenclature_by_id = {
            n.id: n for n in Nomenclature.query.filter(Nomenclature.id.in_(expected.keys())).all()
        }
        rows = sorted(
            (
                {"nomenclature": nomenclature_by_id[nid], "expected_qty": qty}
                for nid, qty in expected.items()
                if nid in nomenclature_by_id
            ),
            key=lambda r: r["nomenclature"].name,
        )
        return render_template("movement/receive.html", doc=doc, rows=rows)

    has_discrepancy = False
    shortage_qty = 0
    total_received_qty = 0
    for nomenclature_id, expected_qty in expected.items():
        received_qty = request.form.get(f"qty_{nomenclature_id}", type=float)
        if received_qty is None or received_qty < 0:
            received_qty = expected_qty
        total_received_qty += received_qty

        plan_line = ShipmentPlanLine.query.filter_by(
            warehouse_id=doc.to_warehouse_id, nomenclature_id=nomenclature_id
        ).first()
        if plan_line and _shipment_is_in_plan(plan_line, doc.shipped_at):
            plan_line.fulfilled_qty += received_qty

        if received_qty != expected_qty:
            has_discrepancy = True
            shortage_qty += max(expected_qty - received_qty, 0)
            db.session.add(
                MovementReceiptDiscrepancy(
                    document_id=doc.id,
                    nomenclature_id=nomenclature_id,
                    expected_qty=expected_qty,
                    received_qty=received_qty,
                )
            )
            _apply_receipt_stock_difference(
                doc, nomenclature_id, expected_qty, received_qty
            )

    doc.sent_qty_snapshot = doc.sent_qty_snapshot or sum(expected.values())
    doc.received_qty_snapshot = total_received_qty
    doc.received_at = datetime.utcnow()
    db.session.commit()
    if shortage_qty:
        flash(
            f"Перемещение {doc.number} принято с недовозом {shortage_qty:g} шт. "
            f"на складе «{doc.to_warehouse.name}»",
            "warning",
        )
    elif has_discrepancy:
        flash(
            f"Перемещение {doc.number} принято с расхождением на складе «{doc.to_warehouse.name}»",
            "warning",
        )
    else:
        flash(f"Перемещение {doc.number} принято на складе МП «{doc.to_warehouse.name}»", "success")
    return redirect(url_for("movement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/toggle-accounting", methods=["POST"])
def toggle_accounting(doc_id):
    """Ручная отметка бухгалтера "внесено в 1С" — просто галочка для
    контроля, никак не влияет на сам документ и не связана с автоматической
    выгрузкой (см. MovementDocument.accounting_entered_at). Отвечает JSON, а
    не редиректом — в списке перемещений эта галочка переключается через
    fetch(), без перезагрузки страницы (список может быть большим, а сама
    отметка ничего в самом документе не меняет)."""
    doc = MovementDocument.query.get_or_404(doc_id)
    doc.accounting_entered_at = None if doc.accounting_entered_at else datetime.utcnow()
    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "checked": doc.accounting_entered_at is not None,
            "at": to_moscow(doc.accounting_entered_at).strftime("%d.%m.%Y %H:%M")
            if doc.accounting_entered_at
            else None,
        }
    )


@bp.route("/<int:doc_id>/toggle-marketplace-request", methods=["POST"])
def toggle_marketplace_request(doc_id):
    """Отметка доступна только после сохранения номера заявки на МП."""
    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.marketplace_request_created_at is None and not (
        doc.marketplace_request_number or ""
    ).strip():
        return jsonify(
            {
                "ok": False,
                "error": "Сначала внесите и сохраните номер заявки на МП",
            }
        ), 400
    doc.marketplace_request_created_at = (
        None if doc.marketplace_request_created_at else datetime.utcnow()
    )
    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "checked": doc.marketplace_request_created_at is not None,
            "at": to_moscow(doc.marketplace_request_created_at).strftime("%d.%m.%Y %H:%M")
            if doc.marketplace_request_created_at
            else None,
        }
    )


@bp.route("/<int:doc_id>/mark-marketplace-request", methods=["POST"])
def mark_marketplace_request(doc_id):
    """Отдельная (не-AJAX) кнопка на детальной странице — тот же флаг, что и
    toggle_marketplace_request в списке (см. MovementDocument.
    marketplace_request_created_at), но обычный редирект вместо JSON: этот
    шаг здесь обязателен перед "Принято на складе" (см. movement.receive),
    поэтому после него страница должна перерисоваться и показать саму
    кнопку приемки, а toggle-в-обратную-сторону тут не нужен."""
    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.marketplace_request_created_at is None:
        if not (doc.marketplace_request_number or "").strip():
            flash("Сначала внесите номер заявки на МП", "danger")
            return redirect(url_for("movement.detail", doc_id=doc.id))
        doc.marketplace_request_created_at = datetime.utcnow()
        db.session.commit()
        flash("Отмечено: заявка на маркетплейс создана", "success")
    return redirect(url_for("movement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/marketplace-request-number", methods=["POST"])
def update_marketplace_request_number(doc_id):
    """Номер заявки обязателен перед установкой галочки "Заявка на МП"."""
    doc = MovementDocument.query.get_or_404(doc_id)
    doc.marketplace_request_number = request.form.get("marketplace_request_number", "").strip() or None
    if doc.marketplace_request_number is None:
        # Нельзя оставить достигнутый этап без номера, по которому документ
        # затем ищут в кабинете маркетплейса и показывают в выгрузках.
        doc.marketplace_request_created_at = None
    db.session.commit()
    if request.form.get("return_to") == "detail":
        return redirect(url_for("movement.detail", doc_id=doc.id))
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
    documents = _visible_movement_query().order_by(MovementDocument.created_at.desc()).all()
    data = export_movement_to_excel(documents)
    fname = f"movements_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


@bp.route("/export-summary.xlsx")
def export_summary():
    """Сводный список перемещений — одна строка на документ (кол-во
    коробов и кол-во товара), а не на каждую позицию, как в export_all —
    для быстрой сверки объемов без разбора по товарам."""
    documents = _visible_movement_query().order_by(MovementDocument.created_at.desc()).all()
    data = export_movement_summary_to_excel(documents)
    fname = f"movements_summary_{timestamp_for_filename()}.xlsx"
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
        _visible_movement_query().filter(MovementDocument.id.in_(doc_ids))
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


@bp.route("/shipping-labels.pdf")
def export_shipping_labels():
    """Стикеры отправления 58x40мм по выбранным в списке перемещениям —
    один стикер на каждый короб документа, с номером ЭТОГО короба (для
    сверки при проклейке), получателем склада назначения (настраивается в
    «Настройки») и отправителем (там же — либо название склада-отправителя
    документа, либо единый текст на все направления)."""
    doc_ids = request.args.getlist("doc_ids", type=int)
    if not doc_ids:
        flash("Выберите хотя бы одно перемещение для печати стикеров", "danger")
        return redirect(url_for("movement.list_documents"))

    documents = (
        _visible_movement_query().filter(MovementDocument.id.in_(doc_ids))
        .order_by(MovementDocument.created_at.desc())
        .all()
    )
    if not documents:
        flash("Перемещения не найдены", "danger")
        return redirect(url_for("movement.list_documents"))

    data = build_movement_shipping_labels_pdf(documents, sender_override=get_shipping_label_sender_override())
    fname = f"shipping_labels_{timestamp_for_filename()}.pdf"
    return Response(
        data,
        mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition(fname, "inline")},
    )


@bp.route("/shipping-label-sender", methods=["POST"])
def update_shipping_label_sender():
    """Единый "Отправитель" для стикеров отправления сразу на все
    направления — чтобы не менять склад-отправитель на каждом из них по
    отдельности. Пустое значение возвращает поведение по умолчанию:
    название фактического склада-отправителя каждого документа."""
    if not current_user.is_admin:
        flash("Настраивать отправителя может только администратор", "danger")
        return redirect(url_for("movement.list_documents"))

    value = request.form.get("sender", "").strip() or None
    setting = AppSetting.query.get(SHIPPING_LABEL_SENDER_KEY)
    if not setting:
        setting = AppSetting(key=SHIPPING_LABEL_SENDER_KEY)
        db.session.add(setting)
    setting.value = value
    db.session.commit()
    flash("Отправитель для стикеров перемещений обновлен", "success")
    return redirect(url_for("auth.users"))
