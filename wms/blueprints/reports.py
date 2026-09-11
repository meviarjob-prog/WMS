from datetime import datetime

from flask import Blueprint, Response, render_template, request

from ..extensions import db
from ..models import (
    BoxItem,
    MovementDocument,
    MovementLine,
    Nomenclature,
    PlacementDocument,
    ReceivingDocument,
    Warehouse,
)
from ..utils.excel_io import (
    export_movement_to_excel,
    export_placement_to_excel,
    export_receiving_to_excel,
    export_shipped_report_to_excel,
    timestamp_for_filename,
)
from ..utils.http import content_disposition

bp = Blueprint("reports", __name__)


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


@bp.route("/")
def index():
    warehouses = Warehouse.query.order_by(Warehouse.code).all()
    return render_template("reports/index.html", warehouses=warehouses)


def _filtered_receiving():
    query = ReceivingDocument.query
    warehouse_id = request.args.get("warehouse_id", type=int)
    date_from = _parse_date(request.args.get("date_from"))
    date_to = _parse_date(request.args.get("date_to"))

    if warehouse_id:
        query = query.filter_by(warehouse_id=warehouse_id)
    if date_from:
        query = query.filter(ReceivingDocument.created_at >= date_from)
    if date_to:
        query = query.filter(ReceivingDocument.created_at < date_to)
    return query.order_by(ReceivingDocument.created_at.desc()).all()


def _filtered_placement():
    query = PlacementDocument.query
    warehouse_id = request.args.get("warehouse_id", type=int)
    date_from = _parse_date(request.args.get("date_from"))
    date_to = _parse_date(request.args.get("date_to"))

    if warehouse_id:
        query = query.filter_by(warehouse_id=warehouse_id)
    if date_from:
        query = query.filter(PlacementDocument.created_at >= date_from)
    if date_to:
        query = query.filter(PlacementDocument.created_at < date_to)
    return query.order_by(PlacementDocument.created_at.desc()).all()


def _filtered_movement():
    query = MovementDocument.query
    warehouse_id = request.args.get("warehouse_id", type=int)
    date_from = _parse_date(request.args.get("date_from"))
    date_to = _parse_date(request.args.get("date_to"))

    if warehouse_id:
        query = query.filter(
            (MovementDocument.from_warehouse_id == warehouse_id)
            | (MovementDocument.to_warehouse_id == warehouse_id)
        )
    if date_from:
        query = query.filter(MovementDocument.created_at >= date_from)
    if date_to:
        query = query.filter(MovementDocument.created_at < date_to)
    return query.order_by(MovementDocument.created_at.desc()).all()


@bp.route("/receiving.xlsx")
def receiving_report():
    documents = _filtered_receiving()
    data = export_receiving_to_excel(documents)
    fname = f"receiving_report_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


@bp.route("/placement.xlsx")
def placement_report():
    documents = _filtered_placement()
    data = export_placement_to_excel(documents)
    fname = f"placement_report_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


@bp.route("/movement.xlsx")
def movement_report():
    documents = _filtered_movement()
    data = export_movement_to_excel(documents)
    fname = f"movement_report_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


def _shipped_rows():
    """Сколько и какого товара реально отгружено (перемещение со склада
    отправки завершено — товар физически уехал) по складам назначения, за
    период. Не путать с «Планом отгрузок» (это план/факт по маркетплейсам)
    — здесь просто фактически отгруженные количества, по любым складам."""
    warehouse_id = request.args.get("warehouse_id", type=int)
    date_from = _parse_date(request.args.get("date_from"))
    date_to = _parse_date(request.args.get("date_to"))

    query = (
        db.session.query(
            MovementDocument.to_warehouse_id,
            BoxItem.nomenclature_id,
            db.func.sum(BoxItem.qty).label("qty"),
        )
        .join(MovementLine, MovementLine.document_id == MovementDocument.id)
        .join(BoxItem, BoxItem.box_id == MovementLine.box_id)
        .filter(MovementDocument.status == "completed")
    )
    if warehouse_id:
        query = query.filter(MovementDocument.to_warehouse_id == warehouse_id)
    if date_from:
        query = query.filter(MovementDocument.completed_at >= date_from)
    if date_to:
        query = query.filter(MovementDocument.completed_at < date_to)

    grouped = query.group_by(MovementDocument.to_warehouse_id, BoxItem.nomenclature_id).all()

    warehouses_by_id = {w.id: w for w in Warehouse.query.all()}
    nomenclature_ids = [r.nomenclature_id for r in grouped]
    items_by_id = (
        {n.id: n for n in Nomenclature.query.filter(Nomenclature.id.in_(nomenclature_ids)).all()}
        if nomenclature_ids
        else {}
    )

    rows = [
        {
            "warehouse": warehouses_by_id.get(r.to_warehouse_id),
            "nomenclature": items_by_id.get(r.nomenclature_id),
            "qty": r.qty,
        }
        for r in grouped
    ]
    rows.sort(
        key=lambda row: (
            row["warehouse"].name if row["warehouse"] else "",
            row["nomenclature"].name if row["nomenclature"] else "",
        )
    )
    return rows


@bp.route("/shipped")
def shipped_report():
    rows = _shipped_rows()
    warehouses = Warehouse.query.order_by(Warehouse.code).all()
    return render_template(
        "reports/shipped.html",
        rows=rows,
        warehouses=warehouses,
        selected_warehouse_id=request.args.get("warehouse_id", type=int),
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
    )


@bp.route("/shipped.xlsx")
def shipped_report_export():
    rows = _shipped_rows()
    data = export_shipped_report_to_excel(rows)
    fname = f"shipped_report_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )
