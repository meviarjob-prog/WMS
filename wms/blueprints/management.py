from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from ..extensions import db
from ..models import (
    MovementDocument,
    MovementReceiptDiscrepancy,
    OneCQuantityCheck,
    PlacementDocument,
    PlacementLine,
    ProductionOrder,
    ProductionRecord,
    ReceivingDocument,
    ReceivingLine,
    ShipmentPlanLine,
)

bp = Blueprint("management", __name__)

# Временно скрыто по просьбе в чате ("пока скрой панель руководителя... будем
# доделывать") — раздел еще дорабатывается совместно с parallel-веткой
# ArturLee. Переключить обратно на False, когда будете готовы показать снова
# (ссылка в base.html скрыта отдельно тем же условием).
HIDDEN_WORK_IN_PROGRESS = True


@bp.before_request
def require_management_access():
    if HIDDEN_WORK_IN_PROGRESS:
        flash("Панель руководителя временно скрыта — дорабатывается", "warning")
        return redirect(url_for("main.index"))
    if not current_user.can_view_management_dashboard():
        flash("Панель руководителя вам не доступна", "danger")
        return redirect(url_for("main.index"))
    return None


def _parse_date(value, fallback):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() if value else fallback
    except ValueError:
        return fallback


def _hours_between(start, finish):
    if not start or not finish or finish < start:
        return None
    return (finish - start).total_seconds() / 3600


def _average(values):
    values = [value for value in values if value is not None]
    return round(sum(values) / len(values), 1) if values else None


def _qty(doc):
    value = doc.total_sent_qty()
    return float(value or 0)


def _warehouse_label(warehouse):
    if warehouse is None:
        return "—"
    marketplace = warehouse.marketplace_label()
    destination = warehouse.marketplace_city or warehouse.name
    return f"{marketplace}: {destination}" if marketplace else destination


@bp.route("/")
def dashboard():
    today = date.today()
    date_from = _parse_date(request.args.get("date_from"), today - timedelta(days=29))
    date_to = _parse_date(request.args.get("date_to"), today)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    start = datetime.combine(date_from, datetime.min.time())
    end = datetime.combine(date_to + timedelta(days=1), datetime.min.time())
    now = datetime.utcnow()

    movements = MovementDocument.query.filter(
        MovementDocument.created_at >= start,
        MovementDocument.created_at < end,
        MovementDocument.status != "merged",
    ).all()
    active_movements = MovementDocument.query.filter(
        MovementDocument.status != "merged",
        MovementDocument.received_at.is_(None),
    ).all()

    movement_stages = {
        "assembly": [d for d in active_movements if d.status in ("draft", "collected")],
        "application": [
            d
            for d in active_movements
            if d.status == "completed" and not d.marketplace_request_created_at
        ],
        "transport": [
            d
            for d in active_movements
            if d.marketplace_request_created_at and not d.shipped_at
        ],
        "transit": [d for d in active_movements if d.shipped_at],
    }
    accepted = [d for d in movements if d.received_at]
    shipped = [d for d in movements if d.shipped_at]

    receiving_docs = ReceivingDocument.query.filter(
        ReceivingDocument.created_at >= start,
        ReceivingDocument.created_at < end,
    ).all()
    active_receiving = ReceivingDocument.query.filter(
        ReceivingDocument.status != "completed"
    ).all()

    received_qty = (
        db.session.query(func.sum(ReceivingLine.qty))
        .join(ReceivingDocument, ReceivingLine.document_id == ReceivingDocument.id)
        .filter(ReceivingDocument.created_at >= start, ReceivingDocument.created_at < end)
        .scalar()
        or 0
    )
    defect_qty = (
        db.session.query(func.sum(ReceivingLine.defect_qty))
        .join(ReceivingDocument, ReceivingLine.document_id == ReceivingDocument.id)
        .filter(ReceivingDocument.created_at >= start, ReceivingDocument.created_at < end)
        .scalar()
        or 0
    )
    placed_qty = (
        db.session.query(func.sum(PlacementLine.qty))
        .join(PlacementDocument, PlacementLine.document_id == PlacementDocument.id)
        .filter(
            PlacementDocument.status == "completed",
            PlacementDocument.completed_at >= start,
            PlacementDocument.completed_at < end,
        )
        .scalar()
        or 0
    )
    produced_qty = ProductionRecord.query.filter(
        ProductionRecord.work_date >= date_from,
        ProductionRecord.work_date <= date_to,
    ).count()

    shortage_qty = (
        db.session.query(
            func.sum(
                MovementReceiptDiscrepancy.expected_qty
                - MovementReceiptDiscrepancy.received_qty
            )
        )
        .join(
            MovementDocument,
            MovementReceiptDiscrepancy.document_id == MovementDocument.id,
        )
        .filter(
            MovementReceiptDiscrepancy.expected_qty
            > MovementReceiptDiscrepancy.received_qty,
            MovementDocument.received_at >= start,
            MovementDocument.received_at < end,
        )
        .scalar()
        or 0
    )

    planned_qty = db.session.query(func.sum(ShipmentPlanLine.planned_qty)).scalar() or 0
    fulfilled_qty = db.session.query(func.sum(ShipmentPlanLine.fulfilled_qty)).scalar() or 0
    plan_percent = min(round(fulfilled_qty / planned_qty * 100, 1), 100) if planned_qty else None

    request_hours = _average(
        _hours_between(d.completed_at, d.marketplace_request_created_at)
        for d in movements
        if d.marketplace_request_created_at
    )
    transport_hours = _average(
        _hours_between(d.marketplace_request_created_at, d.shipped_at)
        for d in movements
        if d.shipped_at
    )
    delivery_hours = _average(
        _hours_between(d.shipped_at, d.received_at) for d in movements if d.received_at
    )

    overdue_application = [
        d
        for d in movement_stages["application"]
        if _hours_between(d.completed_at, now) and _hours_between(d.completed_at, now) > 4
    ]
    overdue_transport = [
        d
        for d in movement_stages["transport"]
        if _hours_between(d.marketplace_request_created_at, now)
        and _hours_between(d.marketplace_request_created_at, now) > 24
    ]
    overdue_transit = [
        d
        for d in movement_stages["transit"]
        if _hours_between(d.shipped_at, now) and _hours_between(d.shipped_at, now) > 48
    ]
    overdue_receiving = [
        d
        for d in active_receiving
        if _hours_between(
            d.sorting_started_at or d.recounting_started_at or d.created_at, now
        )
        and _hours_between(
            d.sorting_started_at or d.recounting_started_at or d.created_at, now
        )
        > 24
    ]
    one_c_issues = OneCQuantityCheck.query.filter(OneCQuantityCheck.checked_at >= start).count()

    alerts = []
    alert_groups = (
        (overdue_application, "danger", "Заявка МП не подана", "Более 4 часов после сборки"),
        (overdue_transport, "warning", "Товар ожидает транспорт", "Более 24 часов после заявки"),
        (overdue_transit, "danger", "Маркетплейс не принял товар", "Более 48 часов в пути"),
    )
    for documents, level, title, subtitle in alert_groups:
        for doc in sorted(documents, key=lambda item: item.created_at)[:4]:
            alerts.append(
                {
                    "level": level,
                    "title": title,
                    "subtitle": subtitle,
                    "number": doc.number,
                    "destination": _warehouse_label(doc.to_warehouse),
                    "qty": _qty(doc),
                    "url": url_for("movement.detail", doc_id=doc.id),
                }
            )
    for doc in sorted(overdue_receiving, key=lambda item: item.created_at)[:3]:
        alerts.append(
            {
                "level": "warning",
                "title": "Приёмка задержана",
                "subtitle": "Более суток без завершения этапа",
                "number": doc.number,
                "destination": doc.warehouse.name if doc.warehouse else "—",
                "qty": float(doc.total_qty() or 0),
                "url": url_for("receiving.detail", doc_id=doc.id),
            }
        )
    alerts = alerts[:8]

    issue_weight = (
        len(overdue_application) * 3
        + len(overdue_transport) * 2
        + len(overdue_transit) * 4
        + len(overdue_receiving) * 2
        + min(int(shortage_qty // 100), 10)
        + min(one_c_issues, 10)
    )
    health_score = max(0, 100 - issue_weight)

    # Этапы до прихода на склад — ведет менеджер в отдельной Google-таблице,
    # синхронизируется в ProductionOrder по кнопке Apps Script (см.
    # production_orders.py). Пока таблицу не подключили (заказов в базе
    # нет вообще), оставляем None — как и раньше, "ожидает подключение".
    production_orders_synced = ProductionOrder.query.count() > 0
    production_stage_counts = (
        dict(
            db.session.query(ProductionOrder.current_stage, func.count(ProductionOrder.id))
            .group_by(ProductionOrder.current_stage)
            .all()
        )
        if production_orders_synced
        else {}
    )
    search_and_sewing_qty = production_stage_counts.get(
        "workshop_search", 0
    ) + production_stage_counts.get("sample_sewing", 0)
    approval_and_card_qty = (
        production_stage_counts.get("sample_approved", 0)
        + production_stage_counts.get("photo_requested", 0)
        + production_stage_counts.get("mp_card_created", 0)
    )
    entering_1c_qty = production_stage_counts.get("data_in_1c", 0) + production_stage_counts.get(
        "order_in_1c", 0
    )

    process_steps = [
        {
            "name": "Поиск цеха / отшив образца",
            "source": "Google Таблицы",
            "state": "ok" if production_orders_synced else "source",
            "value": search_and_sewing_qty if production_orders_synced else None,
        },
        {
            "name": "Согласование / фото / карточка МП",
            "source": "Google Таблицы",
            "state": "ok" if production_orders_synced else "source",
            "value": approval_and_card_qty if production_orders_synced else None,
        },
        {
            "name": "Занесение в 1С",
            "source": "Google Таблицы",
            "state": "ok" if production_orders_synced else "source",
            "value": entering_1c_qty if production_orders_synced else None,
        },
        {"name": "Отшив партии", "source": "WMS / Google", "state": "ok" if produced_qty else "quiet", "value": produced_qty},
        {"name": "Приёмка", "source": "WMS", "state": "risk" if active_receiving else "ok", "value": len(active_receiving)},
        {"name": "Пересчёт", "source": "WMS", "state": "risk" if any(d.status == "recounting" for d in active_receiving) else "ok", "value": sum(d.status == "recounting" for d in active_receiving)},
        {"name": "Разбраковка", "source": "WMS", "state": "risk" if defect_qty else "ok", "value": round(defect_qty)},
        {"name": "Упаковка", "source": "WMS", "state": "ok", "value": round(placed_qty)},
        {"name": "Отгрузка", "source": "WMS", "state": "risk" if overdue_transport or overdue_transit else "ok", "value": round(sum(_qty(d) for d in shipped))},
    ]

    daily = {}
    for offset in range((date_to - date_from).days + 1):
        day = date_from + timedelta(days=offset)
        daily[day] = {"date": day, "production": 0, "receiving": 0, "shipping": 0}
    for day, qty in (
        db.session.query(ProductionRecord.work_date, func.count(ProductionRecord.id))
        .filter(ProductionRecord.work_date >= date_from, ProductionRecord.work_date <= date_to)
        .group_by(ProductionRecord.work_date)
        .all()
    ):
        daily[day]["production"] = qty or 0
    for doc in receiving_docs:
        key = doc.completed_at.date() if doc.completed_at else None
        if key in daily:
            daily[key]["receiving"] += float(doc.total_qty() or 0)
    for doc in shipped:
        key = doc.shipped_at.date()
        if key in daily:
            daily[key]["shipping"] += _qty(doc)
    daily_rows = list(daily.values())[-14:]
    chart_max = max(
        [row[key] for row in daily_rows for key in ("production", "receiving", "shipping")]
        or [1]
    )

    return render_template(
        "management/dashboard.html",
        date_from=date_from,
        date_to=date_to,
        health_score=health_score,
        produced_qty=produced_qty,
        received_qty=received_qty,
        defect_qty=defect_qty,
        defect_percent=round(defect_qty / received_qty * 100, 1) if received_qty else 0,
        shipped_qty=sum(_qty(d) for d in shipped),
        accepted_qty=sum(float(d.total_received_qty() or 0) for d in accepted),
        shortage_qty=shortage_qty,
        plan_percent=plan_percent,
        movement_stages=movement_stages,
        request_hours=request_hours,
        transport_hours=transport_hours,
        delivery_hours=delivery_hours,
        alerts=alerts,
        process_steps=process_steps,
        daily_rows=daily_rows,
        chart_max=chart_max or 1,
        one_c_issues=one_c_issues,
    )
