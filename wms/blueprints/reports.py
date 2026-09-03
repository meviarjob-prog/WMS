from datetime import datetime

from flask import Blueprint, Response, render_template, request

from ..models import MovementDocument, MovementLine, PlacementDocument, ReceivingDocument, Warehouse
from ..utils.excel_io import (
    export_movement_to_excel,
    export_placement_to_excel,
    export_receiving_to_excel,
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
        # склад назначения документа, либо склад-источник хотя бы одного короба в списке
        from_ids = (
            MovementLine.query.filter_by(from_warehouse_id=warehouse_id)
            .with_entities(MovementLine.document_id)
            .distinct()
        )
        query = query.filter(
            (MovementDocument.to_warehouse_id == warehouse_id)
            | (MovementDocument.id.in_(from_ids))
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
