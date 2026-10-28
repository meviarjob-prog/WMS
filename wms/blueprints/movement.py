import hashlib
import hmac
import json
import threading
import time
from datetime import date, datetime, timedelta

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import (
    AppSetting,
    BackgroundJob,
    Box,
    BoxItem,
    Cell,
    CELL_CAPACITY,
    MovementCargoPlace,
    MovementDocument,
    MovementLine,
    MovementReceiptDiscrepancy,
    Nomenclature,
    OzonArticleMapping,
    ShipmentPlanLine,
    User,
    Warehouse,
    UnplacedStock,
    WorkEvent,
)
from ..utils.acceptance_act import (
    ARTICLE_KEY_PREFIX, UNIDENTIFIED_KEY, ActParseError, MAX_ACT_BYTES, parse_act,
)
from ..utils.excel_io import export_movement_summary_to_excel, export_movement_to_excel, timestamp_for_filename
from ..utils.http import content_disposition
from ..utils.numbering import next_number
from ..utils.ozon_api import (
    CARGO_NOT_ACCEPTED_STATUSES,
    CARGO_STATUSES,
    SUPPLY_STATES,
    OzonApiError,
    fetch_bundle_items,
    fetch_cargo_statuses,
    fetch_supply_act_items,
    fetch_supply_cargo_places,
    fetch_supply_orders,
)
from .integration_ozon import get_ozon_credentials
from ..utils.shipping_label_pdf import build_movement_shipping_labels_pdf
from ..utils.timezone import to_moscow
from ..utils.work_log import drop_pick_events, log_pick
from ..utils.waybill_pdf import build_movement_waybills_pdf

bp = Blueprint("movement", __name__)
MOVEMENTS_PAGE_SIZE = 200
# Сборка перемещения завершается автоматически, как только в нем набралось
# столько коробов; следующий короб уходит в новое перемещение на тот же склад.
MOVEMENT_AUTO_COMPLETE_BOXES = 20


def _fulfillment_owns(doc):
    """Фулфилмент (см. чат — склад «ЦЕХ Марат» и подобные) ведет любое
    перемещение со своего склада-отправителя, не только созданное лично
    им — на складе может работать несколько человек. Входящие к ним
    перемещения (to_warehouse_id) этим не открываются — их фулфилмент не
    должен видеть/трогать."""
    return current_user.is_fulfillment_only() and doc.from_warehouse_id == current_user.warehouse_id


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
        or _fulfillment_owns(doc)
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
RECEIVING_ENDPOINTS = {"movement.receive", "movement.receive_act", "movement.receive_via_ozon_api"}
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
            or _fulfillment_owns(doc)
        ):
            abort(404)
        return None
    if request.endpoint in RECEIVING_ENDPOINTS:
        if not current_user.can_receive_movements():
            abort(404)
        return None
    # Остальные изменяющие маршруты (добавить/убрать короб, удалить и
    # т.п.) — только автор, админ или фулфилмент на своем складе
    # (_fulfillment_owns), как и раньше. "Просмотр всех перемещений" здесь
    # не действует (он read-only), а совместный доступ к черновику дальше
    # даем только через route_box_add (у него нет doc_id в URL, этот хук на
    # него не срабатывает) — не через произвольное изменение чужого
    # документа по прямой ссылке.
    if not (current_user.is_admin or doc.created_by_id == current_user.id or _fulfillment_owns(doc)):
        abort(404)
    return None


def _visible_movement_query():
    if current_user.can_view_movements() or current_user.can_receive_movements():
        return MovementDocument.query
    if current_user.is_fulfillment_only():
        # Весь свой склад-отправитель, а не только документы, созданные
        # лично этим пользователем (см. чат).
        return MovementDocument.query.filter(MovementDocument.from_warehouse_id == current_user.warehouse_id)
    return MovementDocument.query.filter(
        or_(MovementDocument.created_by_id == current_user.id, MovementDocument.status.in_(("draft", "collected")))
    )


# Алиасы, по которым слово в поиске понимается как площадка склада-
# получателя, а не как подстрока в названии/городе (Warehouse.marketplace
# хранит "ozon"/"wb", а не "озон"/"вб" — в тексте склада этого слова просто
# нет, иначе оно и так нашлось бы обычным ilike по имени).
_MOVEMENT_SEARCH_MARKETPLACE_ALIASES = {
    "озон": "ozon",
    "ozon": "ozon",
    "вб": "wb",
    "wb": "wb",
}


def _movement_search_token_condition(token):
    """Условие для ОДНОГО слова поискового запроса — совпадение в любом из
    полей (номер, короб, склад-отправитель/получатель, город, площадка,
    автор, № заявки на МП). Несколько слов объединяются через И (см.
    _apply_movement_search) — «191 москва» находит документ, где "191"
    встретилось в одном поле (например, номере короба), а "москва" — в
    другом (город склада), а не требует, чтобы вся фраза целиком была
    подстрокой одного поля."""
    like = f"%{token}%"
    conditions = [
        MovementDocument.number.ilike(like),
        MovementDocument.marketplace_request_number.ilike(like),
        MovementDocument.from_warehouse.has(
            or_(Warehouse.name.ilike(like), Warehouse.marketplace_city.ilike(like))
        ),
        MovementDocument.to_warehouse.has(
            or_(Warehouse.name.ilike(like), Warehouse.marketplace_city.ilike(like))
        ),
        MovementDocument.created_by.has(
            or_(User.username.ilike(like), User.full_name.ilike(like))
        ),
        MovementDocument.lines.any(MovementLine.box.has(Box.box_number.ilike(like))),
    ]
    marketplace = _MOVEMENT_SEARCH_MARKETPLACE_ALIASES.get(token.lower())
    if marketplace:
        conditions.append(
            MovementDocument.to_warehouse.has(Warehouse.marketplace == marketplace)
        )
    return or_(*conditions)


def _apply_movement_search(query):
    """Фильтрует запрос ДО пагинации — поиск идет по всем перемещениям, а
    не только по тем, что попали на текущую страницу (см. чат: раньше
    поле поиска было чисто клиентским JS-фильтром по уже загруженным 50
    строкам — на следующих страницах ничего не находило).

    Запрос из нескольких слов ("191 москва", "озон москва") разбивается по
    пробелам — документ должен совпасть по КАЖДОМУ слову (не обязательно в
    одном и том же поле, см. _movement_search_token_condition), а не
    содержать всю фразу целиком подряд.

    Плюс отдельные фильтры по колонкам (см. чат — "фильтр над каждой
    колонкой") — каждый независим от остальных и от общего поиска q,
    комбинируются через И."""
    q = request.args.get("q", "").strip()
    if q:
        tokens = q.split()
        query = query.filter(
            and_(*(_movement_search_token_condition(token) for token in tokens))
        )
    mp_request = request.args.get("mp_request", "").strip()
    if mp_request == "yes":
        query = query.filter(MovementDocument.marketplace_request_created_at.isnot(None))
    elif mp_request == "no":
        query = query.filter(MovementDocument.marketplace_request_created_at.is_(None))

    number = request.args.get("number", "").strip()
    if number:
        query = query.filter(MovementDocument.number.ilike(f"%{number}%"))

    from_warehouse_id = request.args.get("from_warehouse_id", type=int)
    if from_warehouse_id:
        query = query.filter(MovementDocument.from_warehouse_id == from_warehouse_id)

    to_warehouse_id = request.args.get("to_warehouse_id", type=int)
    if to_warehouse_id:
        query = query.filter(MovementDocument.to_warehouse_id == to_warehouse_id)

    created_by_id = request.args.get("created_by_id", type=int)
    if created_by_id:
        query = query.filter(MovementDocument.created_by_id == created_by_id)

    status = request.args.get("status", "").strip()
    if status:
        query = query.filter(MovementDocument.status == status)

    # _parse_report_date возвращает datetime на полночь указанного дня —
    # дальше сравниваем напрямую, без combine().
    date_from = _parse_report_date(request.args.get("date_from", ""))
    if date_from:
        query = query.filter(MovementDocument.created_at >= date_from)
    date_to = _parse_report_date(request.args.get("date_to", ""))
    if date_to:
        query = query.filter(MovementDocument.created_at < date_to + timedelta(days=1))
    return query


def _movement_filter_options():
    """Списки для выпадающих фильтров над колонками (см. чат) — только
    реально встречающиеся значения, не весь справочник складов/пользователей,
    чтобы в списке не было складов/авторов, которых ни разу не было в
    перемещениях."""
    from_warehouses = (
        Warehouse.query.join(
            MovementDocument, MovementDocument.from_warehouse_id == Warehouse.id
        )
        .filter(Warehouse.is_active.is_(True))
        .distinct()
        .order_by(Warehouse.name)
        .all()
    )
    to_warehouses = (
        Warehouse.query.join(
            MovementDocument, MovementDocument.to_warehouse_id == Warehouse.id
        )
        .distinct()
        .order_by(Warehouse.name)
        .all()
    )
    authors = (
        User.query.join(MovementDocument, MovementDocument.created_by_id == User.id)
        .distinct()
        .order_by(User.full_name)
        .all()
    )
    return {
        "from_warehouses": from_warehouses,
        "to_warehouses": to_warehouses,
        "authors": authors,
    }


def _movement_pagination():
    """Последние перемещения постранично, по MOVEMENTS_PAGE_SIZE документов —
    поиск (см. _apply_movement_search) применяется к запросу до пагинации,
    поэтому находит совпадения по всей истории, а не только на странице."""
    page = max(request.args.get("page", 1, type=int), 1)
    query = _apply_movement_search(_visible_movement_query())
    return (
        query
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


def _committed_by_warehouse_and_item(period_start=None, drafts_only=False):
    """{(warehouse_id, nomenclature_id): кол-во}, уже "закрытое" другими
    коробами, которые едут на этот склад, но еще не отмечены "Принято на
    складе" — черновики перемещения (короб отсканирован, но еще не уехал)
    и уже отправленные, но не принятые (см. movement.receive). После
    приемки это количество уже учтено в fulfilled_qty самой строки плана —
    поэтому принятые сюда не попадают, иначе вычлось бы дважды. Без этого
    remaining_qty продолжал бы показывать полную потребность склада, даже
    если она уже полностью закрыта едущими туда коробами, и подсказка
    маршрутизации короба слала бы туда больше, чем реально нужно."""
    in_progress = or_(
        MovementDocument.status.in_(("draft", "collected")),
        and_(MovementDocument.status == "completed", MovementDocument.received_at.is_(None)),
    )
    if drafts_only:
        # Для целей, посчитанных от остатка на складе-отправителе («готово к
        # отгрузке»): короба, уже уехавшие (completed, еще не принятые), в
        # этот остаток не входят — их повторно вычитать нельзя, учитываем
        # только еще не уехавшие (черновики и собранные).
        in_progress = MovementDocument.status.in_(("draft", "collected"))
    query = (
        db.session.query(
            MovementDocument.to_warehouse_id,
            BoxItem.nomenclature_id,
            func.sum(BoxItem.qty),
        )
        .join(MovementLine, MovementLine.document_id == MovementDocument.id)
        .join(BoxItem, BoxItem.box_id == MovementLine.box_id)
        .filter(in_progress)
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


def _live_overflow_targets(lines):
    """{id строки плана: цель}, пересчитанная ПРЯМО СЕЙЧАС для обычных
    товаров (без приоритета/новинки), у которых план по всем городам уже
    закрыт, а упакованный остаток на складах-отправителях еще есть:
    остаток делится пропорционально доле города в плане (тот же принцип,
    что у shipment_plan._apply_priority_distribution, но по актуальному
    остатку, а не по снимку на момент синхронизации плана — иначе короб,
    собранный после синхронизации, получал «потребности нет»)."""
    by_item = {}
    for line in lines:
        by_item.setdefault(line.nomenclature_id, []).append(line)
    targets = {}
    sender_ids = None
    for nomenclature_id, item_lines in by_item.items():
        if any(l.novelty_marketplace for l in item_lines):
            continue    # новинки — по снимку плана (нужны средние доли городов)
        if any(l.priority is not None for l in item_lines):
            continue    # приоритетные — по снимку плана (distributed_target_qty)
        total_planned = sum(l.planned_qty or 0 for l in item_lines)
        total_open = sum(max((l.planned_qty or 0) - (l.fulfilled_qty or 0), 0) for l in item_lines)
        if total_planned <= 0 or total_open > 0:
            continue
        if sender_ids is None:
            sender_ids = [
                wh.id for wh in Warehouse.query.filter_by(marketplace=None, is_active=True).all()
            ]
        if not sender_ids:
            return {}
        ready = (
            db.session.query(func.sum(BoxItem.qty))
            .join(Box, BoxItem.box_id == Box.id)
            .filter(BoxItem.nomenclature_id == nomenclature_id, Box.warehouse_id.in_(sender_ids))
            .scalar()
        ) or 0
        if ready <= 0:
            continue
        for line in item_lines:
            targets[line.id] = ready * (line.planned_qty or 0) / total_planned
    return targets


def _routing_line_states(box):
    """Состояние каждой строки плана по товарам короба: сколько осталось
    (remaining) и по какому правилу посчитано — общая основа для
    _compute_routing и для блока «Почему так» на экране «Куда везти короб».
    Возвращает (qty_by_item, [состояния])."""
    qty_by_item = {}
    for box_item in box.items:
        qty_by_item[box_item.nomenclature_id] = (
            qty_by_item.get(box_item.nomenclature_id, 0) + box_item.qty
        )
    if not qty_by_item:
        return qty_by_item, []

    lines = ShipmentPlanLine.query.filter(
        ShipmentPlanLine.nomenclature_id.in_(qty_by_item.keys())
    ).all()
    committed_by_period = {}
    drafts_by_period = {}
    live_targets = _live_overflow_targets(lines)

    states = []
    for line in lines:
        period_start = _plan_line_start(line)
        if period_start not in committed_by_period:
            committed_by_period[period_start] = _committed_by_warehouse_and_item(period_start)
            drafts_by_period[period_start] = _committed_by_warehouse_and_item(period_start, drafts_only=True)
        key = (line.warehouse_id, line.nomenclature_id)
        already_committed = committed_by_period[period_start].get(key, 0)
        not_departed = drafts_by_period[period_start].get(key, 0)
        if line.id in live_targets:
            # Цель посчитана ПРЯМО СЕЙЧАС от остатка на складах-отправителях
            # (см. _live_overflow_targets): выполненное и уже уехавшее в
            # цель не входят, вычитаем только еще не уехавшие короба.
            target = live_targets[line.id]
            remaining = max(target - not_departed, 0)
            rule = "доля остатка на складе (по текущему остатку)"
        elif line.distributed_target_qty is not None:
            # Цель из снимка при синхронизации плана — тоже доля остатка на
            # складе, поэтому ни fulfilled_qty, ни «в пути» не вычитаем (см.
            # чат: К-тSmile — цель в разы меньше уже отправленного, и короб
            # с остатком получал «потребности нет»).
            target = line.effective_planned_qty()
            remaining = max(target - not_departed, 0)
            rule = "доля остатка на складе (приоритет/снимок плана)"
        else:
            target = line.planned_qty
            remaining = max(line.planned_qty - line.fulfilled_qty - already_committed, 0)
            rule = "план − выполнено − уже едет"
            if line._blocked_by_novelty_marketplace():
                remaining = 0
                rule = "новинка другой площадки — сюда не отправляем"
        states.append({
            "line": line, "remaining": remaining, "rule": rule, "target": target,
            "committed": already_committed, "drafts": not_departed,
        })
    return qty_by_item, states


def _compute_routing(box):
    """Для содержимого короба ищет склады-города, где по актуальному плану
    отгрузок есть невыполненная потребность (remaining_qty > 0, за вычетом
    уже едущих туда коробов) хотя бы по одному товару из короба —
    группирует по складу и сортирует по тому, сколько из содержимого
    короба реально покрывает эту потребность (matched_qty), по убыванию.
    Пустой список — значит короб никому не нужен по текущему плану (короб
    можно пропустить).

    Для приоритетных товаров (см. ShipmentPlanLine.effective_planned_qty и
    чат) целью служит не жесткий planned_qty конкретного города, а доля
    текущего "готово к отгрузке" пропорционально доле города в общем плане
    по штрихкоду — план по количеству для них не ограничение."""
    qty_by_item, states = _routing_line_states(box)
    by_warehouse = {}
    for state in states:
        line, remaining = state["line"], state["remaining"]
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


def _routing_explanation(box):
    """Строки для блока «Почему так»: по каждому городу плана — план,
    выполнено, едет, в черновиках, цель и итоговая потребность."""
    _, states = _routing_line_states(box)
    rows = []
    for state in states:
        line = state["line"]
        rows.append({
            "warehouse": line.warehouse, "nomenclature": line.nomenclature,
            "planned": line.planned_qty or 0, "fulfilled": line.fulfilled_qty or 0,
            "in_transit": max(state["committed"] - state["drafts"], 0), "drafts": state["drafts"],
            "target": state["target"], "remaining": state["remaining"], "rule": state["rule"],
        })
    rows.sort(key=lambda r: (-r["remaining"], r["warehouse"].name))
    return rows


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
    destination_choices = []
    if current_user.is_main_admin():
        destination_choices = (
            Warehouse.query.filter(Warehouse.is_active.is_(True), Warehouse.marketplace.isnot(None))
            .order_by(Warehouse.marketplace, Warehouse.name)
            .all()
        )
    return render_template(
        "movement/list.html",
        documents=pagination.items,
        pagination=pagination,
        destination_choices=destination_choices,
        route_box_number="",
        route_box=None,
        route_not_found=False,
        routing=[],
        **_movement_filter_options(),
    )


@bp.route("/change-destination", methods=["POST"])
def change_destination():
    """Массовая смена склада назначения у выбранных перемещений — только
    для пользователя «Администратор» (не любого админа). Нужна, чтобы переделать перемещения, ошибочно
    заведенные на WB, на Ozon (и наоборот), не пересобирая документ.

    Правила: принятые на маркетплейсе (received_at) и объединенные
    документы не трогаем (по ним уже зачтено выполнение плана — сначала
    «Вернуть в работу»); у завершенных, но еще не принятых короба
    физически уезжают на новый склад; запомненное сопоставление ГМ Ozon
    сбрасывается (оно относилось к старой поставке); при смене WB ↔ Ozon
    сбрасывается номер заявки на МП (он выдан другим маркетплейсом); в
    Ozon-документе не больше 30 коробов. Документы, уже выгруженные в 1С,
    там автоматически не поправятся — об этом говорим в сообщении."""
    if not current_user.is_main_admin():
        flash("Менять склад назначения может только пользователь «Администратор»", "danger")
        return redirect(url_for("movement.list_documents"))

    doc_ids = request.form.getlist("doc_ids", type=int)
    new_warehouse_id = request.form.get("new_to_warehouse_id", type=int)
    if not doc_ids:
        flash("Выберите перемещения, у которых нужно сменить склад назначения", "danger")
        return redirect(url_for("movement.list_documents"))
    new_warehouse = Warehouse.query.get(new_warehouse_id) if new_warehouse_id else None
    if new_warehouse is None or not new_warehouse.is_active:
        flash("Выберите новый склад назначения", "danger")
        return redirect(url_for("movement.list_documents"))

    changed, skipped, synced_numbers = [], [], []
    for doc in MovementDocument.query.filter(MovementDocument.id.in_(doc_ids)).order_by(MovementDocument.id):
        if doc.to_warehouse_id == new_warehouse.id:
            skipped.append(f"{doc.number} (уже на этот склад)")
            continue
        if new_warehouse.id == doc.from_warehouse_id:
            skipped.append(f"{doc.number} (склад назначения совпал бы с отправителем)")
            continue
        if doc.status == "merged":
            skipped.append(f"{doc.number} (объединен)")
            continue
        if doc.received_at is not None:
            skipped.append(f"{doc.number} (уже принят — сначала «Вернуть в работу»)")
            continue
        all_lines = doc.lines.order_by(MovementLine.id.asc()).all()
        line_count = len(all_lines)
        # В Ozon-перемещении не больше 30 коробов — документ побольше
        # дробим на несколько по 30 (первый остается за исходным номером).
        chunks = [all_lines]
        if new_warehouse.marketplace == "ozon" and line_count > 30:
            if doc.synced_to_1c_at is not None:
                skipped.append(
                    f"{doc.number} ({line_count} коробов, уже выгружен в 1С — дробить нельзя, "
                    "в 1С уйдут дубли; сначала разбейте вручную)"
                )
                continue
            chunks = [all_lines[i:i + 30] for i in range(0, line_count, 30)]

        old_warehouse = doc.to_warehouse
        if doc.status == "completed":
            for line in all_lines:
                box = line.box
                if box.warehouse_id == old_warehouse.id:
                    box.warehouse_id = new_warehouse.id
                    box.warehouse_arrived_at = datetime.utcnow()
                    box.cell_id = None
                    box.zone_id = None
                    box.status = "open"
        elif new_warehouse.marketplace == "ozon":
            if len(chunks[0]) >= 30:
                doc.status = "collected"
        elif doc.status == "collected":
            doc.status = "draft"

        MovementCargoPlace.query.filter_by(document_id=doc.id).delete()
        if (old_warehouse.marketplace or None) != (new_warehouse.marketplace or None):
            doc.marketplace_request_number = None
            doc.marketplace_request_created_at = None
        if doc.synced_to_1c_at is not None:
            synced_numbers.append(doc.number)
        doc.to_warehouse_id = new_warehouse.id
        changed.append(doc.number)
        for chunk in chunks[1:]:
            part = MovementDocument(
                number=next_number("movement"),
                from_warehouse_id=doc.from_warehouse_id,
                to_warehouse_id=new_warehouse.id,
                created_by_id=doc.created_by_id,
                created_at=doc.created_at,
                status=doc.status if doc.status == "completed" else ("collected" if len(chunk) >= 30 else "draft"),
                completed_at=doc.completed_at,
                shipped_at=doc.shipped_at,
                delivery_slot_date=doc.delivery_slot_date,
                marketplace_request_number=doc.marketplace_request_number,
                marketplace_request_created_at=doc.marketplace_request_created_at,
            )
            db.session.add(part)
            db.session.flush()
            moved_box_ids = [line.box_id for line in chunk]
            for line in chunk:
                line.document_id = part.id
            WorkEvent.query.filter(
                WorkEvent.kind == "pick", WorkEvent.ref_kind == "movement", WorkEvent.ref_id == doc.id,
                WorkEvent.box_id.in_(moved_box_ids),
            ).update({"ref_id": part.id}, synchronize_session=False)
            changed.append(f"{part.number} (из {doc.number}, {len(chunk)} кор.)")
        current_app.logger.warning(
            "Склад назначения перемещения %s: «%s» → «%s» (%s)",
            doc.number, old_warehouse.name, new_warehouse.name, current_user.username,
        )
    db.session.commit()

    if changed:
        flash(
            f"Склад назначения изменен на «{new_warehouse.name}»: {', '.join(changed)}. "
            "Если у перемещения был номер заявки на МП другого маркетплейса — он сброшен, внесите заново.",
            "success",
        )
    if synced_numbers:
        flash(
            "Уже выгружены в 1С и там склад автоматически не изменится — поправьте вручную: "
            + ", ".join(synced_numbers),
            "warning",
        )
    if skipped:
        flash("Не изменены: " + "; ".join(skipped), "warning")
    return redirect(url_for("movement.list_documents"))


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
    routing_explain = []
    not_found = False
    if box_number:
        box = Box.find_by_scanned_code(box_number)
        if not box:
            not_found = True
        else:
            routing = _compute_routing(box)
            routing_explain = _routing_explanation(box)

    return render_template(
        "movement/list.html",
        documents=pagination.items,
        pagination=pagination,
        route_box_number=box_number,
        route_box=box,
        route_not_found=not_found,
        routing=routing,
        routing_explain=routing_explain,
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
    log_pick(current_user, box, doc.id)
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
    doc = _open_draft_for_route(box.warehouse_id, to_warehouse_id, current_user.id)

    if doc.lines.filter_by(box_id=box.id).first():
        flash(f"Короб {box.box_number} уже в списке перемещения {doc.number}", "warning")
        return redirect(url_for("movement.list_documents"))

    conflict = _find_conflicting_movement_line(box, exclude_doc_id=doc.id)
    if conflict:
        flash(_conflict_message(box, conflict), "danger")
        return redirect(url_for("movement.list_documents"))

    _create_movement_line(doc, box)
    auto_completed = _auto_complete_if_full(doc)
    try:
        db.session.commit()
    except IntegrityError:
        # Гонка: тот же короб добавил кто-то другой между проверкой выше и
        # этим commit() (см. add_box и чат про документ 202).
        db.session.rollback()
        flash(f"Короб {box.box_number} уже добавлен в перемещение {doc.number} — кем-то другим только что", "danger")
        return redirect(url_for("movement.list_documents"))
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
        + (
            f" В перемещении {MOVEMENT_AUTO_COMPLETE_BOXES} коробов — сборка завершена автоматически, "
            "следующий короб на этот склад попадет в новое перемещение."
            if auto_completed else ""
        ),
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
    box.warehouse_arrived_at = datetime.utcnow()
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
    redirected_from = None
    if doc.status == "collected":
        # Документ уже собран полностью — короб уходит в черновик на тот же склад.
        redirected_from = doc.number
        doc = _open_draft_for_route(doc.from_warehouse_id, doc.to_warehouse_id, current_user.id)
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

    auto_completed = False if editing_after_completion else _auto_complete_if_full(doc)

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
        box.warehouse_arrived_at = datetime.utcnow()
        box.cell_id = None
        box.status = "open"
        if doc.received_at is not None:
            _apply_shipment_fulfillment(box, doc.to_warehouse_id, doc.shipped_at)

    try:
        db.session.commit()
    except IntegrityError:
        # Гонка: кто-то другой добавил этот же короб в тот же документ
        # секундами раньше, между проверкой выше и этим commit() (см. чат —
        # ровно так задвоился короб в документе 202: две MovementLine на
        # один box_id удвоили сумму в экспорте). Уникальный индекс не дал
        # вставить дубль — откатываем и говорим как есть, а не 500-й.
        db.session.rollback()
        flash(f"Короб {box.box_number} уже в этом списке — его только что добавил кто-то другой", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))
    if auto_completed:
        next_doc = _open_draft_for_route(doc.from_warehouse_id, doc.to_warehouse_id, current_user.id)
        db.session.commit()
        flash(
            f"Короб {box.box_number} добавлен. В перемещении {doc.number} {MOVEMENT_AUTO_COMPLETE_BOXES} коробов — "
            f"сборка завершена автоматически. Продолжайте сканировать в новое перемещение {next_doc.number} "
            f"на «{doc.to_warehouse.name}».",
            "success",
        )
        return redirect(url_for("movement.detail", doc_id=next_doc.id))
    elif redirected_from:
        flash(
            f"Перемещение {redirected_from} уже собрано — короб {box.box_number} добавлен "
            f"в перемещение {doc.number} на тот же склад",
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

    drop_pick_events(doc.id, line.box_id)
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
    drop_pick_events(doc.id)
    db.session.delete(doc)
    db.session.commit()
    flash(f"Список перемещения {number} удален", "success")
    return redirect(url_for("movement.list_documents"))


def _complete_document(doc):
    """Завершение сборки: короба переезжают на склад назначения. Без commit —
    вызывающий сам фиксирует транзакцию."""
    for line in doc.lines:
        box = line.box
        box.warehouse_id = doc.to_warehouse_id
        box.warehouse_arrived_at = datetime.utcnow()
        box.cell_id = line.to_cell_id
        box.status = "stored" if line.to_cell_id else "open"
        # Выполнение плана отгрузок засчитывается не здесь, а отдельным
        # действием "Принято на складе" (см. receive()) — короб физически
        # мог еще ехать/лежать непроверенным на складе назначения.

    doc.sent_qty_snapshot = doc.total_item_qty()
    doc.status = "completed"
    doc.completed_at = datetime.utcnow()


def _auto_complete_if_full(doc):
    """Завершает сборку черновика, набравшего MOVEMENT_AUTO_COMPLETE_BOXES
    коробов. Возвращает True, если документ завершен."""
    if doc.status in ("draft", "collected") and doc.lines.count() >= MOVEMENT_AUTO_COMPLETE_BOXES:
        _complete_document(doc)
        return True
    return False


def _open_draft_for_route(from_warehouse_id, to_warehouse_id, created_by_id):
    """Черновик на маршрут: существующий (кем бы ни был начат) или новый."""
    doc = (
        MovementDocument.query.filter_by(
            from_warehouse_id=from_warehouse_id, to_warehouse_id=to_warehouse_id, status="draft"
        )
        .order_by(MovementDocument.created_at.desc())
        .first()
    )
    if doc is None:
        doc = MovementDocument(
            number=next_number("movement"),
            from_warehouse_id=from_warehouse_id,
            to_warehouse_id=to_warehouse_id,
            created_by_id=created_by_id,
        )
        db.session.add(doc)
        db.session.flush()
    return doc


@bp.route("/<int:doc_id>/complete", methods=["POST"])
def complete(doc_id):
    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.status not in ("draft", "collected"):
        flash("Документ уже завершен", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    if doc.lines.count() == 0:
        flash("В списке нет коробов", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    _complete_document(doc)
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
    """Приводит физический остаток к факту приемки (см. чат: расхождение
    при приемке на СЦ должно менять физический остаток, а не только
    записываться).

    Недовоз списывается из содержимого коробов этого перемещения (на
    складе назначения они физически не довезли столько, сколько заявлено).
    Излишек — обратная ситуация: со склада-отправителя физически увезли
    больше, чем было упаковано в короб, поэтому списываем этот излишек с
    неразмещенного остатка отправителя (а не добавляем остаток складу
    назначения — там взялось именно оттуда, не из воздуха). Документ
    сохраняет исходное отправленное количество в sent_qty_snapshot.
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
        UnplacedStock.consume(doc.from_warehouse_id, nomenclature_id, excess)


def _revert_document_receipt(doc):
    """Отменяет учет фактической приемки перед удалением документа. Короба
    и неразмещенный остаток возвращаются как обычно; receipt_changed_boxes
    может быть False только у приемок, принятых в короткий период, когда
    расхождение лишь записывалось без изменения коробов (см. чат) — для
    них короба и тогда не трогали, поэтому и здесь их не трогаем."""
    current = _expected_qty_by_nomenclature(doc)
    discrepancies = {d.nomenclature_id: d for d in doc.discrepancies}
    for nomenclature_id, qty in current.items():
        actual_qty = discrepancies.get(nomenclature_id).received_qty if nomenclature_id in discrepancies else qty
        plan_line = ShipmentPlanLine.query.filter_by(
            warehouse_id=doc.to_warehouse_id, nomenclature_id=nomenclature_id
        ).first()
        if plan_line and _shipment_is_in_plan(plan_line, doc.shipped_at or doc.completed_at):
            plan_line.fulfilled_qty = max(plan_line.fulfilled_qty - actual_qty, 0)

    if doc.receipt_changed_boxes is False:
        return
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
        if discrepancy.excess_qty() and doc.receipt_from_act:
            # При приёмке по акту излишек был добавлен в короб — снимаем.
            left = discrepancy.excess_qty()
            for line in doc.lines.order_by(MovementLine.id.desc()).all():
                if left <= 0:
                    break
                item = BoxItem.query.filter_by(
                    box_id=line.box_id, nomenclature_id=discrepancy.nomenclature_id
                ).first()
                if item:
                    take = min(item.qty, left)
                    item.qty -= take
                    left -= take
                    if item.qty <= 0:
                        db.session.delete(item)
        if discrepancy.excess_qty():
            # Симметрично _apply_receipt_stock_difference: излишек списывался
            # с остатка отправителя — возвращаем его туда же.
            UnplacedStock.add(
                doc.from_warehouse_id,
                discrepancy.nomenclature_id,
                discrepancy.excess_qty(),
            )


@bp.route("/<int:doc_id>/unreceive", methods=["POST"])
def unreceive(doc_id):
    """Администратор возвращает «Принято на складе» в работу, чтобы
    поправить принятое количество: откатывается учет приемки (недовоз
    возвращается в короба, излишек снимается с неразмещенного остатка,
    выполнение плана отгрузок уменьшается), расхождения удаляются. После
    этого кнопка «Принято на складе» снова доступна."""
    if not current_user.is_admin:
        flash("Вернуть приемку в работу может только администратор", "danger")
        return redirect(url_for("movement.detail", doc_id=doc_id))
    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.received_at is None:
        flash("Перемещение еще не принято на складе", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))
    _revert_document_receipt(doc)
    for discrepancy in list(doc.discrepancies):
        db.session.delete(discrepancy)
    doc.received_at = None
    doc.received_qty_snapshot = None
    doc.receipt_changed_boxes = None
    doc.receipt_from_act = None
    doc.receipt_returned_boxes = None
    if doc.synced_to_1c_at is not None:
        doc.composition_changed_at = datetime.utcnow()
    db.session.commit()
    current_app.logger.warning("Приемка перемещения %s возвращена в работу (%s)", doc.number, current_user.username)
    flash(f"Приемка перемещения {doc.number} возвращена в работу — поправьте и снова нажмите «Принято на складе»", "success")
    return redirect(url_for("movement.detail", doc_id=doc.id))


def _receive_precheck(doc):
    """Условия, без которых «Принято на складе» недоступно (общие для
    ручной приемки и приемки по акту). Возвращает redirect или None."""
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
    return None


def _nomenclature_for_act_barcode(code):
    """Товар по баркоду из акта: основной или доп. штрихкод; в отчётах WB
    встречается ведущий ноль ('04640580741533') — сравниваем без него."""
    item = Nomenclature.find_by_barcode(code)
    if item is None and code.lstrip("0") != code:
        item = Nomenclature.find_by_barcode(code.lstrip("0"))
    if item is None and code.isdigit():
        item = Nomenclature.find_by_barcode("0" + code)
    return item


def _nomenclature_for_act_item(key):
    """Позиция акта → товар. Обычный ключ — баркод. Ключ Ozon
    «артикул␟баркод»: сначала по справочнику артикулов Ozon
    (OzonArticleMapping: артикул → баркод → товар), баркод строки — запасной
    путь. Возвращает (товар, описание для сообщения)."""
    if not key.startswith(ARTICLE_KEY_PREFIX):
        return _nomenclature_for_act_barcode(key), key
    article, _, barcode = key[len(ARTICLE_KEY_PREFIX):].partition("\x1f")
    mappings = OzonArticleMapping.query.filter(OzonArticleMapping.article == article).all()
    if not mappings:
        mappings = OzonArticleMapping.query.filter(
            func.lower(OzonArticleMapping.article) == article.lower()
        ).all()
    for mapping in mappings:
        item = _nomenclature_for_act_barcode(mapping.barcode)
        if item is not None:
            return item, article
    return _nomenclature_for_act_barcode(barcode), article


def _reconcile_with_act(doc, act_boxes, key_to_box=None):
    """Сверка акта приёмки с составом коробов перемещения. Ничего не
    записывает. Ключ короба в акте: у WB — наш номер короба, у Ozon —
    штрихкод грузоместа (сопоставление запомнено при выгрузке состава ГМ,
    см. MovementCargoPlace). Возвращает словарь для шаблона и payload
    {номер_короба: {id_товара: принятое_кол-во}} для подтверждения.
    key_to_box — готовое сопоставление ГМ → номер короба (если задано, вместо
    запомненного в БД; так проверка непринятых подставляет сопоставление,
    выведенное по составу)."""
    is_ozon = doc.to_warehouse.marketplace == "ozon"
    result = {
        "boxes": [], "missing": [], "unknown_keys": [], "unknown_barcodes": [],
        "payload": {}, "error": None, "is_ozon": is_ozon,
        "unidentified": [], "ignored_qty": 0, "matched_qty": 0, "unidentified_qty": 0,
        "sent_qty": sum(_expected_qty_by_nomenclature(doc).values()),
    }
    if is_ozon and key_to_box is None:
        key_to_box = {}
        places = MovementCargoPlace.query.filter_by(document_id=doc.id).all()
        if not places:
            result["error"] = (
                "Для Ozon акт сверяется по грузоместам, но для этого перемещения не "
                "запомнено сопоставление ГМ ↔ короб. Сначала выгрузите «Состав ГМ для Ozon»."
            )
            return result
        key_to_box = {p.cargo_barcode: p.box_number for p in places}
    key_to_box = key_to_box or {}

    act_by_box = {}
    barcode_cache = {}
    for key, items in act_boxes.items():
        box_number = key_to_box.get(key) if is_ozon else key
        if box_number is None:
            result["unknown_keys"].append(key)
            result["ignored_qty"] += sum(items.values())
            continue
        target = act_by_box.setdefault(box_number, {})
        for code, qty in items.items():
            if code == UNIDENTIFIED_KEY:
                result["unidentified"].append((box_number, qty))
                result["unidentified_qty"] += qty
                continue
            if code not in barcode_cache:
                barcode_cache[code] = _nomenclature_for_act_item(code)
            nom, label = barcode_cache[code]
            if nom is None:
                if label not in result["unknown_barcodes"]:
                    result["unknown_barcodes"].append(label)
                continue
            target[nom.id] = target.get(nom.id, 0) + qty

    doc_box_numbers = set()
    for line in doc.lines.order_by(MovementLine.id.asc()).all():
        number = line.box.box_number
        doc_box_numbers.add(number)
        expected = {}
        for it in line.box.items:
            expected[it.nomenclature_id] = expected.get(it.nomenclature_id, 0) + it.qty
        if number not in act_by_box:
            result["missing"].append({"line": line, "box_number": number, "total": sum(expected.values())})
            continue
        accepted = act_by_box[number]
        names = {
            n.id: n for n in Nomenclature.query.filter(
                Nomenclature.id.in_(set(expected) | set(accepted))
            ).all()
        }
        rows = []
        for nid in sorted(set(expected) | set(accepted), key=lambda i: names[i].name):
            rows.append({
                "nomenclature": names[nid],
                "expected": expected.get(nid, 0),
                "accepted": accepted.get(nid, 0),
            })
        result["boxes"].append({
            "box_number": number,
            "rows": rows,
            "has_diff": any(r["expected"] != r["accepted"] for r in rows),
        })
        result["payload"][number] = {str(nid): qty for nid, qty in accepted.items()}

    for number in act_by_box:
        if number not in doc_box_numbers:
            result["unknown_keys"].append(number)
            result["ignored_qty"] += sum(act_by_box[number].values())
    result["matched_qty"] = sum(sum(v.values()) for v in result["payload"].values())
    result["payload"] = {k: v for k, v in result["payload"].items()}
    if not result["boxes"]:
        result["error"] = "Ни один короб из акта не найден в этом перемещении — нечего принимать."
    elif result["unknown_barcodes"]:
        result["error"] = (
            "В акте есть товары, которых нет в номенклатуре (для Ozon — артикулы без сопоставления в «Артикулы Ozon»): "
            + ", ".join(result["unknown_barcodes"][:10])
            + ". Добавьте сопоставление артикула/штрихкод и загрузите акт снова."
        )
    return result


def _receive_from_act(doc, payload):
    """Приёмка по акту: короба приводятся к принятому количеству, короба,
    которых нет в акте, снимаются с перемещения и возвращаются на
    склад-отправитель (в план отгрузок не засчитываются). Расхождение
    считается по оставшимся коробам; отправлено (sent_qty_snapshot) — по
    всем исходным, поэтому в списке видно «отправлено (принято)»."""
    lines = doc.lines.order_by(MovementLine.id.asc()).all()
    sent_total = sum(_expected_qty_by_nomenclature(doc).values())

    returned = [l for l in lines if l.box.box_number not in payload]
    kept = [l for l in lines if l.box.box_number in payload]
    if not kept:
        return None
    returned_numbers = [l.box.box_number for l in returned]
    for line in returned:
        _revert_line_effects(doc, line)
        MovementCargoPlace.query.filter_by(document_id=doc.id, box_id=line.box_id).delete()
        db.session.delete(line)
    db.session.flush()

    expected = _expected_qty_by_nomenclature(doc)
    received = {}
    for line in kept:
        accepted = {int(k): float(v) for k, v in payload[line.box.box_number].items()}
        items = {it.nomenclature_id: it for it in line.box.items}
        for nid in set(items) | set(accepted):
            old = items[nid].qty if nid in items else 0
            new = accepted.get(nid, 0)
            if new > old:
                UnplacedStock.consume(doc.from_warehouse_id, nid, new - old)
            if nid in items:
                if new <= 0:
                    db.session.delete(items[nid])
                else:
                    items[nid].qty = new
            elif new > 0:
                db.session.add(BoxItem(box_id=line.box_id, nomenclature_id=nid, qty=new))
            received[nid] = received.get(nid, 0) + new

    for nid in set(expected) | set(received):
        exp_qty = expected.get(nid, 0)
        got_qty = received.get(nid, 0)
        plan_line = ShipmentPlanLine.query.filter_by(
            warehouse_id=doc.to_warehouse_id, nomenclature_id=nid
        ).first()
        if plan_line and _shipment_is_in_plan(plan_line, doc.shipped_at):
            plan_line.fulfilled_qty += got_qty
        if got_qty != exp_qty:
            db.session.add(MovementReceiptDiscrepancy(
                document_id=doc.id, nomenclature_id=nid, expected_qty=exp_qty, received_qty=got_qty,
            ))

    doc.sent_qty_snapshot = sent_total
    doc.received_qty_snapshot = sum(received.values())
    doc.receipt_changed_boxes = True
    doc.receipt_from_act = True
    doc.receipt_returned_boxes = ", ".join(returned_numbers) or None
    doc.received_at = datetime.utcnow()
    if doc.synced_to_1c_at is not None:
        doc.composition_changed_at = datetime.utcnow()
    db.session.commit()
    return {"returned": returned_numbers, "sent": sent_total, "received": sum(received.values())}


@bp.route("/<int:doc_id>/receive/act", methods=["POST"])
def receive_act(doc_id):
    """Шаг 1 приёмки по акту: загрузка файла акта и экран сверки (ничего не
    записывается до подтверждения на этом экране)."""
    doc = MovementDocument.query.get_or_404(doc_id)
    blocked = _receive_precheck(doc)
    if blocked is not None:
        return blocked
    upload = request.files.get("act_file")
    if upload is None or not upload.filename:
        flash("Выберите файл акта приёмки (.xlsx или .zip)", "danger")
        return redirect(url_for("movement.receive", doc_id=doc.id))
    data = upload.read(MAX_ACT_BYTES + 1)
    try:
        act_boxes, act_total = parse_act(data, prefer_article=doc.to_warehouse.marketplace == "ozon")
    except ActParseError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("movement.receive", doc_id=doc.id))
    rec = _reconcile_with_act(doc, act_boxes)
    return render_template(
        "movement/receive_act.html", doc=doc, rec=rec, act_total=act_total,
        filename=upload.filename, payload_json=json.dumps(rec["payload"], ensure_ascii=False),
    )


def _render_receive_form(doc, expected, received_by_nomenclature=None, ozon_totals=None):
    """Строит строки и рендерит форму ручной приемки (movement/receive.html).
    "Отправлено" — всегда expected; значение по умолчанию в поле "Принято
    по факту" — из received_by_nomenclature, если передано (например,
    после сверки с Ozon API на расхождение, см. receive_via_ozon_api —
    чат: "после проверки кол-во принято по факту вносится автоматически",
    чтобы не перебивать совпавшие позиции руками), иначе как раньше равно
    expected (обычная приемка без расхождений)."""
    received_by_nomenclature = received_by_nomenclature or {}
    extra_ids = set(ozon_totals or {}) - set(expected)
    nomenclature_by_id = {
        n.id: n for n in Nomenclature.query.filter(Nomenclature.id.in_(set(expected) | extra_ids)).all()
    }
    rows = sorted(
        (
            {
                "nomenclature": nomenclature_by_id[nid],
                "expected_qty": qty,
                "received_qty": received_by_nomenclature.get(nid, qty),
                "ozon_qty": None if ozon_totals is None else ozon_totals.get(nid, 0),
            }
            for nid, qty in expected.items()
            if nid in nomenclature_by_id
        ),
        # Расхождения по Ozon — наверх, чтобы не искать их в длинном списке.
        key=lambda r: (
            r["ozon_qty"] is None or r["ozon_qty"] == r["expected_qty"],
            r["nomenclature"].name,
        ),
    )
    # Товар, который Ozon принял, а мы не отправляли — показываем для
    # сведения (в форме его не принять: в коробах его нет).
    extra_rows = sorted(
        (
            {"nomenclature": nomenclature_by_id[nid], "ozon_qty": ozon_totals[nid]}
            for nid in extra_ids
            if nid in nomenclature_by_id and ozon_totals[nid]
        ),
        key=lambda r: r["nomenclature"].name,
    )
    return render_template(
        "movement/receive.html", doc=doc, rows=rows, extra_rows=extra_rows,
        show_ozon=ozon_totals is not None,
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
    blocked = _receive_precheck(doc)
    if blocked is not None:
        return blocked

    expected = _expected_qty_by_nomenclature(doc)
    if not expected:
        flash("В списке нет коробов с товаром", "danger")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    if request.method == "GET":
        return _render_receive_form(doc, expected)

    if request.form.get("act_payload"):
        try:
            payload = json.loads(request.form["act_payload"])
            assert isinstance(payload, dict)
        except (ValueError, AssertionError):
            flash("Данные акта повреждены — загрузите акт заново", "danger")
            return redirect(url_for("movement.receive", doc_id=doc.id))
        outcome = _receive_from_act(doc, payload)
        if outcome is None:
            flash("Ни один короб из акта не найден в этом перемещении", "danger")
            return redirect(url_for("movement.receive", doc_id=doc.id))
        msg = (
            f"Перемещение {doc.number} принято по акту: принято {outcome['received']:g} из "
            f"{outcome['sent']:g} шт."
        )
        if outcome["returned"]:
            msg += f" Возвращено на склад «{doc.from_warehouse.name}» коробов: {len(outcome['returned'])}."
        flash(msg, "warning" if outcome["received"] != outcome["sent"] else "success")
        return redirect(url_for("movement.detail", doc_id=doc.id))

    # Товар, которого вообще не было заявлено в отправке (короба не
    # ожидали), но по факту его привезли на СЦ — излишек не из заявленного
    # (см. чат). Строка добавляется на этой же форме через автокомплит,
    # expected_qty=0 заставляет остальной код (расхождение, зачисление
    # излишка на отправителя) сработать как для обычного излишка.
    for raw_id in request.form.getlist("extra_nomenclature_id"):
        try:
            extra_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if extra_id in expected:
            continue
        qty = request.form.get(f"qty_{extra_id}", type=float)
        if not qty or qty <= 0:
            continue
        if not Nomenclature.query.get(extra_id):
            continue
        expected[extra_id] = 0.0

    received_by_nomenclature = {
        nomenclature_id: request.form.get(f"qty_{nomenclature_id}", type=float)
        for nomenclature_id in expected
    }
    has_discrepancy, shortage_qty, total_received_qty = _apply_receive(doc, expected, received_by_nomenclature)
    if has_discrepancy:
        flash(
            f"Перемещение {doc.number} принято на складе «{doc.to_warehouse.name}»: "
            + _discrepancy_summary(doc),
            "warning",
        )
    else:
        flash(f"Перемещение {doc.number} принято на складе МП «{doc.to_warehouse.name}»", "success")
    return redirect(url_for("movement.detail", doc_id=doc.id))


def _discrepancy_summary(doc):
    """«недовоз 5 шт., излишек 3 шт.» по записанным расхождениям документа."""
    db.session.refresh(doc)
    parts = []
    if doc.total_shortage_qty():
        parts.append(f"недовоз {doc.total_shortage_qty():g} шт.")
    if doc.total_excess_qty():
        parts.append(f"излишек {doc.total_excess_qty():g} шт.")
    return ", ".join(parts) or "без расхождений"


def _apply_receive(doc, expected, received_by_nomenclature):
    """Общее ядро приемки — используется и ручной формой (receive(), ввод
    количества по каждому товару), и автоприемкой по API Ozon (см.
    receive_via_ozon_api), когда расхождения нет совсем. received_by_nomenclature —
    {nomenclature_id: количество}; отсутствующий/отрицательный/None элемент
    — считается как expected_qty (без расхождения), как и раньше при
    незаполненном поле формы.

    Возвращает (has_discrepancy, shortage_qty, total_received_qty)."""
    has_discrepancy = False
    shortage_qty = 0
    total_received_qty = 0
    for nomenclature_id, expected_qty in expected.items():
        received_qty = received_by_nomenclature.get(nomenclature_id)
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
            _apply_receipt_stock_difference(doc, nomenclature_id, expected_qty, received_qty)

    # Перезаписываем всегда, а не только если было пусто: expected посчитан
    # заново в начале receive(), по текущему содержимому короба на момент
    # приемки. Если короб поправили МЕЖДУ завершением сборки (complete(),
    # где снимок берется первый раз) и приемкой, старый снимок с completion
    # иначе навсегда остался бы устаревшим и расходился бы с расхождением,
    # посчитанным по уже новому содержимому (см. чат: бейдж "Излишек" при
    # видимом недовозе в скобках).
    doc.sent_qty_snapshot = sum(expected.values())
    doc.received_qty_snapshot = total_received_qty
    doc.receipt_changed_boxes = True
    doc.received_at = datetime.utcnow()
    db.session.commit()
    return has_discrepancy, shortage_qty, total_received_qty


def _nomenclature_for_ozon_sku_info(sku_info):
    """Товар по данным строки акта Ozon (sku_info — барcode/offer_id/sku
    из ответа /v1/supply-order/act/product/get). Сначала по штрихкоду
    (_nomenclature_for_act_barcode — та же логика, что и для файла акта),
    запасной путь — артикул Ozon через OzonArticleMapping, как и в
    _nomenclature_for_act_item для файла."""
    barcode = sku_info.get("barcode") or ""
    item = _nomenclature_for_act_barcode(barcode) if barcode else None
    if item is not None:
        return item
    offer_id = sku_info.get("offer_id") or ""
    if not offer_id:
        return None
    mappings = OzonArticleMapping.query.filter(OzonArticleMapping.article == offer_id).all()
    if not mappings:
        mappings = OzonArticleMapping.query.filter(
            func.lower(OzonArticleMapping.article) == offer_id.lower()
        ).all()
    for mapping in mappings:
        item = _nomenclature_for_act_barcode(mapping.barcode)
        if item is not None:
            return item
    return None


def _fetch_ozon_accepted_totals(doc, timeout=None, max_retries=None, deadline=None):
    """Сводное фактически принятое количество по каждому товару этого
    перемещения через API Ozon (/v1/supply-order/act/product/get) — БЕЗ
    разбивки по коробам, которую этот метод не отдает вообще (см. чат —
    сверено по официальному Swagger-спеку Ozon). Поэтому используется
    только для автоприемки "все сошлось целиком" (см.
    receive_via_ozon_api) — решить, какие именно короба не доехали, по
    этим данным нельзя.

    timeout/max_retries пробрасываются в вызовы Ozon API как есть — см.
    RECEIVE_REVIEW_CALL_TIMEOUT в receive_review_run: интерактивный вызов
    не должен зависать на полных повторах (до 5 попыток по 60с — свыше
    5 минут на один проблемный запрос, см. чат про 504 от реверс-прокси).

    Возвращает (totals_by_nomenclature_id, ошибка) — при ошибке первое
    значение None, вызывающая сторона не должна автопринимать."""
    client_id, api_key = get_ozon_credentials()
    order_number = (doc.marketplace_request_number or "").strip()
    if not order_number:
        return None, "у перемещения не указан номер заявки на маркетплейс"
    try:
        orders = fetch_supply_orders(
            client_id, api_key, order_number=order_number, days_back=None,
            timeout=timeout, max_retries=max_retries, deadline=deadline,
        )
    except OzonApiError as exc:
        return None, str(exc)
    if not orders:
        return None, f"заявка «{order_number}» не найдена в Ozon API"

    totals = {}
    unmapped = set()
    for order in orders:
        for supply in order.get("supplies") or []:
            supply_id = supply.get("supply_id")
            if not supply_id:
                continue
            try:
                items, unidentified = fetch_supply_act_items(
                    client_id, api_key, supply_id, timeout=timeout, max_retries=max_retries, deadline=deadline,
                )
            except OzonApiError as exc:
                return None, str(exc)
            if unidentified:
                return None, f"в поставке {supply_id} есть неопознанные излишки ({unidentified:g} шт)"
            for item in items:
                nom = _nomenclature_for_ozon_sku_info(item)
                fact_qty = item["fact_quantity"]
                if nom is None:
                    if fact_qty:
                        unmapped.add(item["barcode"] or item["offer_id"] or "?")
                    continue
                totals[nom.id] = totals.get(nom.id, 0) + fact_qty
    if unmapped:
        return None, "товары без сопоставления в номенклатуре: " + ", ".join(sorted(unmapped)[:10])
    return totals, None


def _compute_ozon_mismatches(expected, totals):
    """Сравнивает отправленное (expected) с фактически принятым по Ozon API
    (totals) по каждому товару — список (nomenclature_id, expected_qty,
    got_qty) только для несовпавших позиций. Используется и одиночной
    автоприемкой (receive_via_ozon_api), и массовой проверкой
    (receive_review_run)."""
    mismatches = []
    for nomenclature_id, expected_qty in expected.items():
        got_qty = totals.get(nomenclature_id, 0)
        if got_qty != expected_qty:
            mismatches.append((nomenclature_id, expected_qty, got_qty))
    for nomenclature_id in set(totals) - set(expected):
        if totals[nomenclature_id]:
            mismatches.append((nomenclature_id, 0, totals[nomenclature_id]))
    return mismatches


# Сколько максимум ждем Ozon при построении таблицы грузомест (все запросы).
OZON_CARGOES_DEADLINE = 45


def _cargo_barcode_key(value):
    text = str(value or "").strip().upper()
    digits = "".join(ch for ch in text if ch.isdigit())
    return text, digits


def _ozon_cargo_report(doc):
    """Грузоместа поставки по Ozon API со статусами и сопоставлением с
    нашими коробами — только для просмотра, ничего не меняет.

    Сопоставление: сначала по штрихкоду ГМ из запомненного «Состава ГМ»
    (MovementCargoPlace), иначе по составу — что заявлено в грузоместе
    (/v1/supply-order/bundle) совпадает с содержимым ровно одного короба.
    Принятого количества по грузоместу Ozon не отдает — только статус."""
    report = {"rows": [], "unmatched_boxes": [], "error": None, "not_accepted": 0, "supplies": []}
    order_number = (doc.marketplace_request_number or "").strip()
    if not order_number:
        report["error"] = "у перемещения не указан номер заявки на маркетплейс"
        return report
    client_id, api_key = get_ozon_credentials()
    deadline = time.monotonic() + OZON_CARGOES_DEADLINE
    common = dict(timeout=RECEIVE_REVIEW_CALL_TIMEOUT, max_retries=RECEIVE_REVIEW_CALL_MAX_RETRIES, deadline=deadline)
    try:
        orders = fetch_supply_orders(client_id, api_key, order_number=order_number, days_back=None, **common)
        supply_ids = [
            int(supply["supply_id"])
            for order in orders for supply in (order.get("supplies") or []) if supply.get("supply_id")
        ]
        if not supply_ids:
            report["error"] = f"заявка «{order_number}» не найдена в Ozon API"
            return report
        report["supplies"] = supply_ids
        places = fetch_supply_cargo_places(client_id, api_key, supply_ids, **common)
        statuses = fetch_cargo_statuses(
            client_id, api_key,
            {sid: [c["cargo_id"] for c in cargoes if c["cargo_id"]] for sid, cargoes in places.items()},
            **common,
        )
    except OzonApiError as exc:
        report["error"] = str(exc)
        return report

    lines = doc.lines.order_by(MovementLine.id.asc()).all()
    contents = {}
    for line in lines:
        box_items = {}
        for item in line.box.items:
            box_items[item.nomenclature_id] = box_items.get(item.nomenclature_id, 0) + item.qty
        contents[line.box_id] = (line.box.box_number, box_items)

    saved = {}
    for place in MovementCargoPlace.query.filter_by(document_id=doc.id).all():
        text, digits = _cargo_barcode_key(place.cargo_barcode)
        saved[text] = place.box_id
        if digits:
            saved.setdefault("#" + digits, place.box_id)

    used_boxes = set()
    rows = []
    for supply_id, cargoes in places.items():
        for cargo in cargoes:
            info = statuses.get(cargo["cargo_id"], {})
            status = info.get("status") or "UNSPECIFIED"
            text, digits = _cargo_barcode_key(cargo["barcode"])
            box_id = saved.get(text) or (saved.get("#" + digits) if digits else None)
            if box_id not in contents or box_id in used_boxes:
                box_id = None
            rows.append({
                "supply_id": supply_id,
                "cargo_id": cargo["cargo_id"],
                "barcode": cargo["barcode"],
                "bundle_id": cargo["bundle_id"] or info.get("bundle_id") or "",
                "status": status,
                "status_label": CARGO_STATUSES.get(status, status),
                "not_accepted": status in CARGO_NOT_ACCEPTED_STATUSES,
                "box_id": box_id,
                "how": "по штрихкоду ГМ" if box_id else None,
                "declared_qty": None,
            })
            if rows[-1]["box_id"]:
                used_boxes.add(box_id)

    # Остальные — по составу грузоместа.
    lookup = {}
    for row in rows:
        if row["box_id"] or not row["bundle_id"]:
            continue
        if time.monotonic() > deadline:
            report["error"] = "Ozon отвечал долго — часть грузомест сопоставить по составу не успели"
            break
        try:
            items = fetch_bundle_items(client_id, api_key, row["bundle_id"], **common)
        except OzonApiError as exc:
            report["error"] = f"состав грузоместа не получен: {exc}"
            break
        declared = {}
        unknown = False
        for item in items:
            key = (item["barcode"], item["offer_id"])
            if key not in lookup:
                lookup[key] = _nomenclature_for_ozon_sku_info(item)
            nom = lookup[key]
            if nom is None:
                unknown = True
                continue
            declared[nom.id] = declared.get(nom.id, 0) + item["quantity"]
        row["declared_qty"] = sum(declared.values())
        if unknown or not declared:
            continue
        candidates = [bid for bid, (_, box_items) in contents.items() if box_items == declared and bid not in used_boxes]
        if len(candidates) == 1:
            row["box_id"] = candidates[0]
            row["how"] = "по составу"
            used_boxes.add(candidates[0])
        elif len(candidates) > 1:
            row["how"] = f"одинаковый состав у {len(candidates)} коробов"

    for row in rows:
        if row["box_id"]:
            number, box_items = contents[row["box_id"]]
            row["box_number"] = number
            row["box_qty"] = sum(box_items.values())
        else:
            row["box_number"] = None
            row["box_qty"] = None
    rows.sort(key=lambda r: (not r["not_accepted"], r["box_number"] or "~", str(r["barcode"])))
    report["rows"] = rows
    report["not_accepted"] = sum(1 for r in rows if r["not_accepted"])
    report["unmatched_boxes"] = [
        {"box_number": number, "qty": sum(box_items.values())}
        for bid, (number, box_items) in contents.items() if bid not in used_boxes
    ]
    return report


@bp.route("/<int:doc_id>/ozon-cargoes")
def ozon_cargoes(doc_id):
    """Таблица «грузоместо Ozon — наш короб — статус» (HTML-фрагмент,
    подгружается кнопкой в карточке перемещения и в ручной проверке)."""
    doc = MovementDocument.query.get_or_404(doc_id)
    if not (current_user.is_admin or current_user.can_receive_movements()):
        return "Недостаточно прав", 403
    if doc.to_warehouse.marketplace != "ozon":
        return '<div class="text-muted small">Грузоместа есть только у поставок на Ozon.</div>'
    return render_template("movement/_ozon_cargoes.html", doc=doc, report=_ozon_cargo_report(doc))


@bp.route("/<int:doc_id>/receive/ozon-api", methods=["POST"])
def receive_via_ozon_api(doc_id):
    """Автоприемка по данным Ozon API — без разбора по коробам: если то,
    что Ozon показывает как фактически принятое, СОВПАДАЕТ ПОЛНОСТЬЮ с
    тем, что отправили (по каждому товару), подтверждаем приемку как есть
    — как обычная кнопка "Принято на складе" без расхождений. Если есть
    хоть какое-то расхождение — не принимаем автоматически: понять, какие
    именно короба не доехали, по этим данным нельзя (нет разбивки по
    грузоместам, см. _fetch_ozon_accepted_totals), поэтому решать нужно
    вручную — обычной формой или актом файлом (см. чат)."""
    doc = MovementDocument.query.get_or_404(doc_id)
    blocked = _receive_precheck(doc)
    if blocked is not None:
        return blocked
    if doc.to_warehouse.marketplace != "ozon":
        flash("Автоприемка по API доступна только для перемещений на Ozon", "danger")
        return redirect(url_for("movement.receive", doc_id=doc.id))

    expected = _expected_qty_by_nomenclature(doc)
    if not expected:
        flash("В списке нет коробов с товаром", "danger")
        return redirect(url_for("movement.receive", doc_id=doc.id))

    totals, error = _fetch_ozon_accepted_totals(doc)
    if error:
        flash(f"Не удалось свериться с Ozon API: {error}. Примите вручную.", "danger")
        return redirect(url_for("movement.receive", doc_id=doc.id))

    mismatches = _compute_ozon_mismatches(expected, totals)

    if mismatches and _is_identified_surplus_only(mismatches):
        _, _, total_received_qty = _accept_with_identified_surplus(doc, expected, totals)
        flash(
            f"Перемещение {doc.number} принято по данным Ozon API — {total_received_qty:g} шт., "
            f"{_discrepancy_summary(doc)}; излишек добавлен в короб и списан с остатка отправителя.",
            "success",
        )
        return redirect(url_for("movement.detail", doc_id=doc.id))

    if mismatches:
        flash(
            f"Ozon API показывает расхождение по {len(mismatches)} товар(ам) — они выделены в таблице ниже. "
            "Автоматически принять нельзя: разбивки по коробам у Ozon API нет, непонятно, какой короб не "
            "доехал. «Принято по факту» уже проставлено по данным Ozon — проверьте и подтвердите, "
            "или поправьте вручную.",
            "warning",
        )
        received = {nid: totals.get(nid, 0) for nid in expected}
        return _render_receive_form(doc, expected, received, ozon_totals=totals)

    _, _, total_received_qty = _apply_receive(doc, expected, expected)
    flash(
        f"Перемещение {doc.number} принято по данным Ozon API — совпало полностью, {total_received_qty:g} шт.",
        "success",
    )
    return redirect(url_for("movement.detail", doc_id=doc.id))


# Сколько дней после даты слота приемки выжидаем перед проверкой через
# Ozon API (чат: "также в исключения добавим текущий день минус 2 дня на
# приемку") — Ozon обрабатывает приемку поставки не мгновенно в день
# слота, и сверка раньше срока видела бы ложную недостачу (товар еще
# физически не разобран и не внесен в систему Ozon).
RECEIVE_REVIEW_SLOT_GRACE_DAYS = 2


def _pending_ozon_receive_documents():
    """Завершенные перемещения на Ozon, еще не принятые на складе, —
    кандидаты для массовой проверки (см. receive_review/receive_review_run).
    Исключены (чат: "исключить... даты слотов не наступивших и также
    перемещения без числовых номеров заявки", позже — "текущий день минус
    2 дня на приемку"):
      - без номера заявки или с номером не из одних цифр (например "fbs" —
        попадает в это поле не как настоящий номер поставки Ozon и ведет
        только к ошибке/лишнему запросу к API, см. чат про такие записи);
      - с датой слота приемки (delivery_slot_date) позже, чем
        RECEIVE_REVIEW_SLOT_GRACE_DAYS дней назад — включая и еще не
        наступившие даты, и совсем недавние, которые Ozon может еще не
        успеть обработать; такие даты заполняются вручную заранее, поэтому
        сами по себе НЕ являются признаком ошибки (см. отличие от записей
        без номера заявки выше)."""
    cutoff = date.today() - timedelta(days=RECEIVE_REVIEW_SLOT_GRACE_DAYS)
    candidates = (
        MovementDocument.query.join(Warehouse, MovementDocument.to_warehouse_id == Warehouse.id)
        .filter(
            MovementDocument.status == "completed",
            MovementDocument.received_at.is_(None),
            Warehouse.marketplace == "ozon",
            MovementDocument.marketplace_request_number.isnot(None),
            MovementDocument.marketplace_request_number != "",
        )
        .order_by(MovementDocument.shipped_at.asc())
        .all()
    )
    return [
        doc
        for doc in candidates
        if doc.marketplace_request_number.strip().isdigit()
        and (doc.delivery_slot_date is None or doc.delivery_slot_date <= cutoff)
    ]


def _find_single_missing_box(doc, mismatches):
    """Проверяет, объясняется ли расхождение ровно ОДНИМ недоехавшим
    коробом (см. чат: "проверить все ли короба, если короб отсутствует —
    вернуть его на склад"). API Ozon не отдает разбивку по коробам
    (_fetch_ozon_accepted_totals), поэтому это лишь предположение по сумме:
    если нет ни одного излишка (только недостача) и ровно один короб
    документа по составу СОВПАДАЕТ С НЕДОСТАЧЕЙ ТОЧНО — считаем, что не
    доехал именно он. Если есть излишек, или недостача не сходится ровно с
    одним коробом (несколько кандидатов или вообще ни одного) — считать
    однозначно нельзя, нужна ручная проверка (возвращает None)."""
    required = {}
    for nomenclature_id, expected_qty, got_qty in mismatches:
        if got_qty > expected_qty:
            return None
        shortage = expected_qty - got_qty
        if shortage:
            required[nomenclature_id] = shortage
    if not required:
        return None
    matches = []
    for line in doc.lines.order_by(MovementLine.id.asc()).all():
        box_contents = {}
        for item in line.box.items:
            box_contents[item.nomenclature_id] = box_contents.get(item.nomenclature_id, 0) + item.qty
        if box_contents == required:
            matches.append(line)
    if len(matches) == 1:
        return matches[0]
    return None


def _return_single_box_and_accept_rest(doc, line):
    """Возвращает один короб на склад-отправитель как недоехавший (тот же
    прием, что и у возврата коробов, не найденных в акте — см.
    _receive_from_act) и принимает остальное как есть: после удаления
    строки оставшееся содержимое документа по построению (см.
    _find_single_missing_box) уже равно тому, что показал Ozon API,
    поэтому расхождение не записывается."""
    box_number = line.box.box_number
    _revert_line_effects(doc, line)
    MovementCargoPlace.query.filter_by(document_id=doc.id, box_id=line.box_id).delete()
    db.session.delete(line)
    db.session.flush()
    expected = _expected_qty_by_nomenclature(doc)
    _apply_receive(doc, expected, expected)
    doc.receipt_returned_boxes = box_number
    db.session.commit()
    return box_number


def _is_identified_surplus_only(mismatches):
    """Расхождение только излишком (по каждому товару принято не меньше
    отправленного). Товары тут всегда опознаны: неопознанные позиции и
    товары без сопоставления _fetch_ozon_accepted_totals отдает ошибкой."""
    return bool(mismatches) and all(got_qty >= expected_qty for _, expected_qty, got_qty in mismatches)


def _accept_with_identified_surplus(doc, expected, totals):
    """Приемка по количествам Ozon (totals). Недостачу _apply_receive
    списывает из коробов как обычно. Приемка с излишком (см. чат: "если товар принят излишком — добавить
    его, если его можно идентифицировать"). Излишек добавляется в короб
    перемещения (в тот, где этот товар уже есть, иначе в последний) и,
    как и при ручной приемке, списывается с неразмещенного остатка
    отправителя; расхождение записывается. receipt_from_act — чтобы
    «Вернуть в работу» снял добавленное из коробов обратно."""
    lines = doc.lines.order_by(MovementLine.id.asc()).all()
    full_expected = dict(expected)
    for nomenclature_id, got_qty in totals.items():
        expected_qty = full_expected.setdefault(nomenclature_id, 0)
        excess = got_qty - expected_qty
        if excess <= 0 or not lines:
            continue
        target = None
        for line in reversed(lines):
            target = BoxItem.query.filter_by(box_id=line.box_id, nomenclature_id=nomenclature_id).first()
            if target is not None:
                break
        if target is not None:
            target.qty += excess
        else:
            db.session.add(BoxItem(box_id=lines[-1].box_id, nomenclature_id=nomenclature_id, qty=excess))
    received = {nid: totals.get(nid, 0) for nid in full_expected}
    doc.receipt_from_act = True
    if doc.synced_to_1c_at is not None:
        doc.composition_changed_at = datetime.utcnow()
    return _apply_receive(doc, full_expected, received)


def _recent_shortages(days=60):
    """{id товара: [(номер перемещения, недостача)]} по уже принятым за
    последние дни перемещениям — вдруг излишек здесь это недостача там."""
    since = datetime.utcnow() - timedelta(days=days)
    found = {}
    rows = (
        db.session.query(MovementDocument.number, MovementReceiptDiscrepancy)
        .join(MovementReceiptDiscrepancy, MovementReceiptDiscrepancy.document_id == MovementDocument.id)
        .filter(MovementDocument.received_at >= since)
        .all()
    )
    for number, discrepancy in rows:
        if discrepancy.received_qty < discrepancy.expected_qty:
            found.setdefault(discrepancy.nomenclature_id, []).append(
                (number, discrepancy.expected_qty - discrepancy.received_qty)
            )
    return found


def _annotate_possible_mixups(manual, names):
    """Подсказка для ручной проверки (см. чат: "возможно, в таком же
    кол-ве товар всплыл или не хватило в другом перемещении — ошибка при
    отправке"): если в ДРУГОМ перемещении из этой же проверки по тому же
    товару нашлось расхождение ровно противоположное и той же величины —
    похоже, короб по ошибке попал не в ту поставку при отправке. Ничего не
    меняет — только добавляет текстовую подсказку entry["hints"], решение
    все равно за человеком."""
    deltas = []
    for idx, entry in enumerate(manual):
        for nomenclature_id, expected_qty, got_qty in entry["mismatches"]:
            delta = got_qty - expected_qty
            if delta:
                deltas.append((idx, nomenclature_id, delta))
    for idx, nomenclature_id, delta in deltas:
        for other_idx, other_nid, other_delta in deltas:
            if other_idx == idx or other_nid != nomenclature_id or other_delta != -delta:
                continue
            other_doc = manual[other_idx]["doc"]
            name = names.get(nomenclature_id, nomenclature_id)
            kind = "Недостача" if delta < 0 else "Излишек"
            other_kind = "излишком" if delta < 0 else "недостачей"
            manual[idx].setdefault("hints", []).append(
                f"{kind} «{name}» {abs(delta):g} шт. совпадает по величине с {other_kind} в "
                f"перемещении {other_doc.number} — возможно, короб по ошибке попал не в ту "
                f"поставку при отправке."
            )


# Сколько документов проверять за один вызов (см. чат: 504 от реверс-прокси
# при проверке сразу всех неприятых документов — на каждый нужно 2-4 живых
# запроса к Ozon API, и при большом количестве накопившихся документов
# суммарное время легко превышает таймаут прокси). Остальные отмеченные
# документы проверяются следующим нажатием кнопки "Продолжить проверку".
RECEIVE_REVIEW_BATCH_LIMIT = 8

# Бюджет на весь вызов и на одну попытку запроса к Ozon API (см. чат: 504
# повторялся даже при выборе всего 8 документов — обычные ozon_api.REQUEST_TIMEOUT=60с
# и MAX_RETRIES=5 дают больше 5 минут на ОДИН зависший/ошибающийся вызов, и
# батч тут не спасает. Здесь — жестче: максимум 3 попытки по 10с на вызов
# (2 попытки оказалось мало — при обычном 429 "слишком много запросов" от
# Ozon это всего 1 повтор, не хватает пережить кратковременное
# ограничение, см. чат про /v1/supply-order/act/product/get: 429),
# и как только суммарное время по всем уже обработанным документам
# превышает RECEIVE_REVIEW_TIME_BUDGET, обработка следующих документов из
# пачки останавливается (они уходят в "остальные ждут проверки" вместе с
# теми, что не поместились в RECEIVE_REVIEW_BATCH_LIMIT) — так единственный
# проблемный документ не утягивает за собой весь запрос.
RECEIVE_REVIEW_TIME_BUDGET = 25.0
RECEIVE_REVIEW_CALL_TIMEOUT = 10
RECEIVE_REVIEW_CALL_MAX_RETRIES = 3


@bp.route("/receive-review", methods=["GET"])
def receive_review():
    """Админская страница массовой проверки неприятых перемещений на Ozon
    (см. чат: "нужна кнопка для роли админ для проверки всех непринятых
    перемешений", позже — "надо сделать так чтобы выбирать перемещения
    для проверки": список с чекбоксами, проверяются только отмеченные)."""
    if not current_user.is_admin:
        flash("Массовая проверка приемки доступна только администратору", "danger")
        return redirect(url_for("movement.list_documents"))
    return render_template(
        "movement/receive_review.html",
        docs=_pending_ozon_receive_documents(),
        batch_limit=RECEIVE_REVIEW_BATCH_LIMIT,
        slot_grace_days=RECEIVE_REVIEW_SLOT_GRACE_DAYS,
    )


# Статусы заявок Ozon для списка проверки — кэш на несколько минут, чтобы
# повторное открытие страницы не гоняло десятки запросов к API.
OZON_STATES_CACHE_SECONDS = 300
OZON_STATES_DEADLINE = 40
_ozon_states_cache = {"at": None, "states": {}, "requested": set()}

# Цвет значка статуса: приемка закончена — зеленый, идет — голубой,
# спор/отказ/отмена — красный.
OZON_STATE_BADGE = {
    "COMPLETED": "bg-success",
    "REPORTS_CONFIRMATION_AWAITING": "bg-success",
    "ACCEPTANCE_AT_STORAGE_WAREHOUSE": "bg-info text-dark",
    "IN_TRANSIT": "bg-warning text-dark",
    "ACCEPTED_AT_SUPPLY_WAREHOUSE": "bg-warning text-dark",
    "READY_TO_SUPPLY": "bg-secondary",
    "DATA_FILLING": "bg-secondary",
    "REPORT_REJECTED": "bg-danger",
    "REJECTED_AT_SUPPLY_WAREHOUSE": "bg-danger",
    "CANCELLED": "bg-danger",
    "OVERDUE": "bg-danger",
}


@bp.route("/receive-review/ozon-states")
def receive_review_ozon_states():
    """Статусы заявок Ozon для перемещений из списка массовой проверки —
    одной выборкой списка заявок (/v3/supply-order/list + get), а не
    запросом на каждое перемещение. Отдается JSON, страница подгружает его
    после открытия, чтобы не ждать API при загрузке."""
    if not current_user.is_admin:
        return jsonify(ok=False, error="Только для администратора"), 403
    docs = _pending_ozon_receive_documents()
    numbers = {doc.marketplace_request_number.strip() for doc in docs}
    cache = _ozon_states_cache
    fresh = (
        cache["at"] is not None
        and time.monotonic() - cache["at"] < OZON_STATES_CACHE_SECONDS
        and numbers <= cache["requested"]
        and not request.args.get("refresh")
    )
    error = None
    if not fresh and numbers:
        client_id, api_key = get_ozon_credentials()
        oldest = min((doc.shipped_at or doc.completed_at or doc.created_at) for doc in docs)
        days_back = min(max((datetime.utcnow() - oldest).days + 14, 30), 365)
        try:
            orders = fetch_supply_orders(
                client_id, api_key, days_back=days_back,
                timeout=RECEIVE_REVIEW_CALL_TIMEOUT, max_retries=RECEIVE_REVIEW_CALL_MAX_RETRIES,
                deadline=time.monotonic() + OZON_STATES_DEADLINE, wanted_numbers=numbers, partial_ok=True,
            )
        except OzonApiError as exc:
            orders = None
            error = str(exc)
        if orders is not None:
            states = {}
            for order in orders:
                number = str(order.get("order_number") or "").strip()
                if number and number not in states:
                    states[number] = order.get("state") or "UNSPECIFIED"
            cache["states"] = states
            cache["requested"] = set(numbers)
            cache["at"] = time.monotonic()
    states = cache["states"]
    result = {}
    for doc in docs:
        state = states.get(doc.marketplace_request_number.strip())
        result[doc.id] = {
            "state": state,
            "label": SUPPLY_STATES.get(state, state) if state else ("—" if error else "не найдена"),
            "badge": OZON_STATE_BADGE.get(state, "bg-secondary") if state else "bg-light text-muted border",
        }
    return jsonify(ok=error is None, error=error, states=result)


RECEIVE_REVIEW_JOB_KIND = "ozon_receive_review"
# Поток проверки отмечается «жив» каждые RECEIVE_REVIEW_HEARTBEAT_SECONDS;
# без отметки дольше RECEIVE_REVIEW_JOB_STALE_SECONDS (сервер перезапустили,
# поток погиб) проверка считается оборванной — страница не висит вечно.
RECEIVE_REVIEW_HEARTBEAT_SECONDS = 10
RECEIVE_REVIEW_JOB_STALE_SECONDS = 120
# Сколько максимум ждем Ozon по одному перемещению (все запросы вместе):
# дальше оно уходит в ручную проверку с причиной «Ozon не ответил вовремя».
RECEIVE_REVIEW_DOC_DEADLINE = 90
# Если ход проверки не сдвигается дольше этого (поток жив, но застрял),
# проверка помечается зависшей.
RECEIVE_REVIEW_JOB_STALL_SECONDS = 300


def _run_receive_review(docs, time_budget=None, on_progress=None):
    """Ядро массовой проверки (см. receive_review_run). Возвращает
    сериализуемый результат — его можно и сразу показать, и сохранить для
    фоновой проверки:
      auto_accepted: [doc_id], box_returned: [[doc_id, короб]],
      surplus_accepted / manual: [{doc_id, reason, rows: [[nid, отпр., принято]], hints}],
      remaining_ids: [doc_id] — не успели проверить (бюджет времени)."""
    start = time.monotonic()
    auto_accepted, box_returned, manual, surplus_candidates, surplus_accepted = [], [], [], [], []
    processed = 0
    for doc in docs:
        # Бюджет времени не применяется к первому документу — иначе при
        # уже истекшем бюджете пачка могла бы не обработать вообще ничего.
        if time_budget is not None and processed and time.monotonic() - start > time_budget:
            break
        processed += 1
        doc_started = time.monotonic()
        try:
            db.session.refresh(doc)
            if doc.received_at is not None:
                continue      # уже приняли (вручную или другой проверкой) — не трогаем
            expected = _expected_qty_by_nomenclature(doc)
            if not expected:
                manual.append({"doc": doc, "reason": "в перемещении нет коробов с товаром", "mismatches": []})
                continue
            totals, error = _fetch_ozon_accepted_totals(
                doc, timeout=RECEIVE_REVIEW_CALL_TIMEOUT, max_retries=RECEIVE_REVIEW_CALL_MAX_RETRIES,
                deadline=time.monotonic() + RECEIVE_REVIEW_DOC_DEADLINE,
            )
            if error:
                manual.append({"doc": doc, "reason": error, "mismatches": []})
                continue
            mismatches = _compute_ozon_mismatches(expected, totals)
            if not mismatches:
                _apply_receive(doc, expected, expected)
                auto_accepted.append(doc.id)
                continue
            all_rows = [
                (nid, expected.get(nid, 0), totals.get(nid, 0)) for nid in set(expected) | set(totals)
            ]
            if _is_identified_surplus_only(mismatches):
                # Решаем после всей пачки: если такой же недостачей отозвалось
                # другое перемещение — это не излишек, а ошибка при отправке.
                surplus_candidates.append(
                    {"doc": doc, "reason": None, "mismatches": mismatches, "rows": all_rows,
                     "expected": expected, "totals": totals}
                )
                continue
            missing_line = _find_single_missing_box(doc, mismatches)
            if missing_line is not None:
                box_returned.append([doc.id, _return_single_box_and_accept_rest(doc, missing_line)])
                continue
            manual.append({"doc": doc, "reason": None, "mismatches": mismatches, "rows": all_rows})
        except Exception as exc:  # noqa: BLE001 — один сбойный документ не должен рушить остальные
            db.session.rollback()
            current_app.logger.exception("Проверка приемки Ozon: ошибка на %s", doc.number)
            manual.append({"doc": doc, "reason": f"ошибка при проверке: {exc}", "mismatches": []})
        finally:
            spent = time.monotonic() - doc_started
            if spent > 30:
                current_app.logger.warning(
                    "Проверка приемки Ozon: %s проверялся %.0f с", doc.number, spent
                )
            if on_progress is not None:
                on_progress(processed)

    names = {
        n.id: n.name for n in Nomenclature.query.filter(
            Nomenclature.id.in_({nid for e in manual + surplus_candidates for nid, _, _ in e.get("rows", [])})
        ).all()
    }
    _annotate_possible_mixups(manual + surplus_candidates, names)
    recent_shortages = _recent_shortages() if surplus_candidates else {}
    for entry in surplus_candidates:
        for nid, expected_qty, got_qty in entry["mismatches"]:
            excess = got_qty - expected_qty
            for number, shortage in recent_shortages.get(nid, []):
                if abs(shortage - excess) < 1e-6 and number != entry["doc"].number:
                    entry.setdefault("hints", []).append(
                        f"Излишек «{names.get(nid, nid)}» {excess:g} шт. совпадает с недостачей в уже "
                        f"принятом перемещении {number} — возможно, товар уехал не в ту поставку."
                    )
        if entry.get("hints"):
            entry["reason"] = "Излишек похож на ошибку при отправке — проверьте вручную"
            manual.append(entry)
            continue
        _accept_with_identified_surplus(entry["doc"], entry["expected"], entry["totals"])
        surplus_accepted.append(entry)

    def _serialize(entry):
        return {
            "doc_id": entry["doc"].id,
            "reason": entry.get("reason"),
            "rows": [list(r) for r in entry.get("rows", [])],
            "hints": entry.get("hints", []),
        }

    return {
        "auto_accepted": auto_accepted,
        "box_returned": box_returned,
        "surplus_accepted": [_serialize(e) for e in surplus_accepted],
        "manual": [_serialize(e) for e in manual],
        "remaining_ids": [doc.id for doc in docs[processed:]],
    }


def _ozon_rows_signature(doc_id, rows):
    """Подпись строк сверки (товар, отправлено, принято по Ozon) для кнопки
    «Подтвердить приемку»: количество нельзя подправить в форме, а принять
    можно только то, что показано на экране."""
    normalized = sorted([int(nid), float(exp), float(got)] for nid, exp, got in rows)
    raw = json.dumps({"doc": int(doc_id), "rows": normalized}, sort_keys=True)
    key = str(current_app.config.get("SECRET_KEY") or "").encode()
    return hmac.new(key, raw.encode(), hashlib.sha256).hexdigest()


@bp.route("/<int:doc_id>/receive/ozon-confirm", methods=["POST"])
def receive_ozon_confirm(doc_id):
    """Кнопка «Подтвердить приемку» в результате массовой проверки: принять
    перемещение ровно по данным Ozon, показанным в таблице (недостача
    списывается из коробов, излишек добавляется в короб и списывается с
    остатка отправителя). Если состав коробов изменился после проверки —
    не принимаем, нужно проверить заново."""
    return_to = request.form.get("return_to") or ""
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = url_for("movement.receive_review")
    if not current_user.is_admin:
        flash("Подтвердить приемку по проверке может только администратор", "danger")
        return redirect(return_to)
    doc = MovementDocument.query.get_or_404(doc_id)
    blocked = _receive_precheck(doc)
    if blocked is not None:
        return redirect(return_to)
    try:
        rows = json.loads(request.form.get("rows") or "[]")
        rows = [(int(nid), float(exp), float(got)) for nid, exp, got in rows]
    except (TypeError, ValueError):
        rows = []
    sig = request.form.get("sig") or ""
    if not rows or not hmac.compare_digest(sig, _ozon_rows_signature(doc.id, rows)):
        flash(f"{doc.number}: данные проверки повреждены — запустите проверку заново", "danger")
        return redirect(return_to)
    expected = _expected_qty_by_nomenclature(doc)
    shown_expected = {nid: exp for nid, exp, _ in rows if exp}
    if {nid: qty for nid, qty in expected.items() if qty} != shown_expected:
        flash(f"{doc.number}: состав коробов изменился после проверки — запустите проверку заново", "danger")
        return redirect(return_to)
    totals = {nid: got for nid, _, got in rows}
    _, _, total_received_qty = _accept_with_identified_surplus(doc, expected, totals)
    flash(
        f"Перемещение {doc.number} принято по данным Ozon: {total_received_qty:g} шт. — {_discrepancy_summary(doc)}",
        "success",
    )
    return redirect(return_to)


def _render_receive_review_result(result, extra_remaining_ids=(), job=None):
    """Показ результата (_run_receive_review) — свежего или сохраненного."""
    doc_ids = set(result["auto_accepted"]) | {d for d, _ in result["box_returned"]}
    doc_ids |= {e["doc_id"] for e in result["surplus_accepted"] + result["manual"]}
    docs = {d.id: d for d in MovementDocument.query.filter(MovementDocument.id.in_(doc_ids or [0])).all()}
    nids = {r[0] for e in result["surplus_accepted"] + result["manual"] for r in e["rows"]}
    noms = {n.id: n for n in Nomenclature.query.filter(Nomenclature.id.in_(nids or [0])).all()}

    def _hydrate(entry):
        return {
            "doc": docs.get(entry["doc_id"]),
            "reason": entry["reason"],
            "hints": entry["hints"],
            "rows_json": json.dumps(entry["rows"]),
            "sig": _ozon_rows_signature(entry["doc_id"], entry["rows"]) if entry["rows"] else None,
            "diff_rows": sorted(
                ((noms[nid], exp, got) for nid, exp, got in entry["rows"] if nid in noms),
                key=lambda r: r[0].name,
            ),
        }

    remaining_ids = list(result["remaining_ids"]) + list(extra_remaining_ids)
    return render_template(
        "movement/receive_review_result.html",
        auto_accepted=[docs[i] for i in result["auto_accepted"] if i in docs],
        box_returned=[(docs[i], box) for i, box in result["box_returned"] if i in docs],
        surplus_accepted=[e for e in map(_hydrate, result["surplus_accepted"]) if e["doc"]],
        manual=[e for e in map(_hydrate, result["manual"]) if e["doc"]],
        remaining=len(remaining_ids),
        remaining_ids=remaining_ids,
        batch_limit=RECEIVE_REVIEW_BATCH_LIMIT,
        job=job,
        return_to=url_for("movement.receive_review_job", job_id=job.id) if job else url_for("movement.receive_review"),
    )


def _running_review_job():
    job = (
        BackgroundJob.query.filter_by(kind=RECEIVE_REVIEW_JOB_KIND, status="running")
        .order_by(BackgroundJob.id.desc())
        .first()
    )
    if job is None:
        return None
    now = datetime.utcnow()
    last_seen = job.heartbeat_at or job.started_at
    last_progress = job.progress_at or job.started_at
    error = None
    if now - last_seen > timedelta(seconds=RECEIVE_REVIEW_JOB_STALE_SECONDS):
        error = "проверка оборвалась — скорее всего, сервер перезапускался"
    elif now - last_progress > timedelta(seconds=RECEIVE_REVIEW_JOB_STALL_SECONDS):
        error = (
            f"проверка зависла на перемещении {job.current_label or '?'} — "
            f"больше {RECEIVE_REVIEW_JOB_STALL_SECONDS // 60} минут без продвижения"
        )
    if error:
        job.status = "failed"
        job.error = error
        job.finished_at = now
        db.session.commit()
        return None
    return job


def _job_heartbeat(app, job_id, stop):
    """Отдельный поток: пока идет проверка, раз в несколько секунд
    отмечает задачу живой (сам поток проверки может надолго уйти в запрос
    к Ozon API)."""
    with app.app_context():
        while not stop.wait(RECEIVE_REVIEW_HEARTBEAT_SECONDS):
            try:
                BackgroundJob.query.filter_by(id=job_id, status="running").update(
                    {"heartbeat_at": datetime.utcnow()}
                )
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
        db.session.remove()


class _ReviewJobStopped(Exception):
    """Фоновую проверку пометили оборванной — поток должен остановиться."""


def _receive_review_background(app, job_id, doc_ids):
    stop = threading.Event()
    threading.Thread(target=_job_heartbeat, args=(app, job_id, stop), daemon=True).start()
    try:
        _receive_review_background_body(app, job_id, doc_ids)
    finally:
        stop.set()


def _receive_review_background_body(app, job_id, doc_ids):
    with app.app_context():
        try:
            order = {doc_id: i for i, doc_id in enumerate(doc_ids)}
            docs = sorted(
                (d for d in _pending_ozon_receive_documents() if d.id in order), key=lambda d: order[d.id]
            )
            job = BackgroundJob.query.get(job_id)
            job.progress_total = len(docs)
            db.session.commit()

            def _progress(done):
                status = db.session.query(BackgroundJob.status).filter_by(id=job_id).scalar()
                if status != "running":
                    # Сторож уже пометил проверку зависшей (и, возможно,
                    # запущена новая) — дальше не проверяем, чтобы не
                    # принимать те же перемещения параллельно.
                    raise _ReviewJobStopped()
                label = docs[done].number if done < len(docs) else None
                now = datetime.utcnow()
                BackgroundJob.query.filter_by(id=job_id).update(
                    {"progress_done": done, "current_label": label, "heartbeat_at": now, "progress_at": now}
                )
                db.session.commit()

            _progress(0)
            result = _run_receive_review(docs, on_progress=_progress)
            job = BackgroundJob.query.get(job_id)
            job.result = json.dumps(result)
            job.status = "done"
        except _ReviewJobStopped:
            db.session.rollback()
            db.session.remove()
            return
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            app.logger.exception("Фоновая проверка приемки Ozon не удалась")
            job = BackgroundJob.query.get(job_id)
            job.status = "failed"
            job.error = str(exc)[:2000]
        job.finished_at = datetime.utcnow()
        db.session.commit()
        db.session.remove()


@bp.route("/receive-review/run", methods=["POST"])
def receive_review_run():
    """Выполняет проверку отмеченных на странице /receive-review
    документов (doc_ids в форме): что сошлось полностью по данным Ozon API
    — принимает как есть; где недостача ровно на один короб — вернет этот
    короб и примет остальное; где только излишек опознанного товара —
    добавит его в короб и примет (если такой же недостачи нет в другом
    перемещении); все прочее (недостача вместе с излишком, неоднозначная
    недостача, ошибки API) — в список на ручную проверку, с подсказкой при
    похожем расхождении в другом перемещении (см. _annotate_possible_mixups).

    На сервере проверка идет в фоне (страница /receive-review/job/<id>
    показывает ход и затем результат) — живые запросы к Ozon API по
    нескольким документам не укладывались в таймаут веб-сервера (504).
    В тестах (RECEIVE_REVIEW_INLINE) — сразу, пачками по
    RECEIVE_REVIEW_BATCH_LIMIT с бюджетом времени, как раньше."""
    if not current_user.is_admin:
        flash("Массовая проверка приемки доступна только администратору", "danger")
        return redirect(url_for("movement.list_documents"))

    try:
        selected_ids = [int(v) for v in request.form.getlist("doc_ids")]
    except ValueError:
        selected_ids = []
    if not selected_ids:
        flash("Выберите хотя бы одно перемещение для проверки", "warning")
        return redirect(url_for("movement.receive_review"))

    if current_app.config.get("RECEIVE_REVIEW_INLINE", current_app.config.get("TESTING")):
        selected = [doc for doc in _pending_ozon_receive_documents() if doc.id in set(selected_ids)]
        batch = selected[:RECEIVE_REVIEW_BATCH_LIMIT]
        result = _run_receive_review(batch, time_budget=RECEIVE_REVIEW_TIME_BUDGET)
        return _render_receive_review_result(
            result, extra_remaining_ids=[doc.id for doc in selected[RECEIVE_REVIEW_BATCH_LIMIT:]]
        )

    running = _running_review_job()
    if running is not None:
        flash("Проверка уже идет — дождитесь ее окончания", "warning")
        return redirect(url_for("movement.receive_review_job", job_id=running.id))
    job = BackgroundJob(
        kind=RECEIVE_REVIEW_JOB_KIND, created_by_id=current_user.id,
        progress_total=len(selected_ids), params=json.dumps(selected_ids),
        heartbeat_at=datetime.utcnow(),
    )
    db.session.add(job)
    db.session.commit()
    threading.Thread(
        target=_receive_review_background,
        args=(current_app._get_current_object(), job.id, selected_ids),
        daemon=True,
    ).start()
    return redirect(url_for("movement.receive_review_job", job_id=job.id))


@bp.route("/receive-review/job/<int:job_id>")
def receive_review_job(job_id):
    if not current_user.is_admin:
        flash("Массовая проверка приемки доступна только администратору", "danger")
        return redirect(url_for("movement.list_documents"))
    job = BackgroundJob.query.filter_by(id=job_id, kind=RECEIVE_REVIEW_JOB_KIND).first_or_404()
    if job.status == "running":
        _running_review_job()  # помечает оборвавшуюся
        db.session.refresh(job)
    if job.status == "done":
        return _render_receive_review_result(json.loads(job.result), job=job)
    remaining_ids = []
    if job.status == "failed":
        try:
            wanted = set(json.loads(job.params or "[]"))
        except ValueError:
            wanted = set()
        remaining_ids = [d.id for d in _pending_ozon_receive_documents() if d.id in wanted]
    return render_template("movement/receive_review_progress.html", job=job, remaining_ids=remaining_ids)


@bp.route("/receive-review/job/<int:job_id>/status")
def receive_review_job_status(job_id):
    if not current_user.is_admin:
        return jsonify(status="forbidden"), 403
    job = BackgroundJob.query.filter_by(id=job_id, kind=RECEIVE_REVIEW_JOB_KIND).first_or_404()
    if job.status == "running":
        _running_review_job()  # помечает оборвавшуюся
        db.session.refresh(job)
    return jsonify(
        status=job.status, done=job.progress_done, total=job.progress_total, current=job.current_label or ""
    )


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


@bp.route("/<int:doc_id>/delivery-slot-date", methods=["POST"])
def update_delivery_slot_date(doc_id):
    """Дата слота, забронированного на маркетплейсе для приемки (см. чат) —
    вносится вручную, используется вместо сегодняшней даты при печати
    стикеров отправления (см. utils.shipping_label_pdf)."""
    doc = MovementDocument.query.get_or_404(doc_id)
    raw = request.form.get("delivery_slot_date", "").strip()
    if raw:
        try:
            doc.delivery_slot_date = date.fromisoformat(raw)
        except ValueError:
            flash("Некорректная дата поставки", "danger")
            if request.form.get("return_to") == "detail":
                return redirect(url_for("movement.detail", doc_id=doc.id))
            return redirect(url_for("movement.list_documents"))
    else:
        doc.delivery_slot_date = None
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


def _parse_report_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


@bp.route("/export-summary.xlsx")
def export_summary():
    """Сводный список перемещений — одна строка на документ (кол-во
    коробов и кол-во товара), а не на каждую позицию, как в export_all —
    для быстрой сверки объемов без разбора по товарам. Вторая страница
    файла — отгрузки по дням (см. export_movement_summary_to_excel).

    date_from/date_to (необязательные, формат YYYY-MM-DD) ограничивают
    отчет документами, отгруженными в этот период (по MovementDocument.
    shipped_at — когда транспорт физически забрал товар); без них в отчет
    попадают все документы, как раньше."""
    query = _visible_movement_query()
    date_from = _parse_report_date(request.args.get("date_from", ""))
    date_to = _parse_report_date(request.args.get("date_to", ""))
    if date_from:
        query = query.filter(MovementDocument.shipped_at >= date_from)
    if date_to:
        query = query.filter(MovementDocument.shipped_at < date_to + timedelta(days=1))
    documents = query.order_by(MovementDocument.created_at.desc()).all()
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
    """Стикеры отправления 58x40мм — по одному документу (со страницы
    перемещения) или по выбранным в списке — один стикер на каждый короб
    документа: отправитель (настраивается в «Настройки» — либо склад
    документа, либо единый текст на все направления), направление,
    порядковый номер короба из общего количества, дата поставки (см.
    MovementDocument.delivery_slot_date — если не заполнена, дата печати)
    и площадка склада назначения."""
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
