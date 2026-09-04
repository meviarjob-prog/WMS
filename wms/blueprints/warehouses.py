from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..extensions import db
from ..models import Cell, Warehouse, Zone
from ..utils.numbering import next_number

bp = Blueprint("warehouses", __name__)


@bp.route("/")
def list_warehouses():
    warehouses = Warehouse.query.order_by(Warehouse.code).all()
    return render_template("warehouses/list.html", warehouses=warehouses)


@bp.route("/create", methods=["POST"])
def create_warehouse():
    name = request.form.get("name", "").strip()
    address = request.form.get("address", "").strip()

    if not name:
        flash("Укажите наименование склада", "danger")
        return redirect(url_for("warehouses.list_warehouses"))

    wh = Warehouse(code=next_number("warehouse"), name=name, address=address)
    db.session.add(wh)
    db.session.commit()
    flash(f"Склад «{name}» создан ({wh.code})", "success")
    return redirect(url_for("warehouses.list_warehouses"))


@bp.route("/<int:warehouse_id>/toggle", methods=["POST"])
def toggle_warehouse(warehouse_id):
    wh = Warehouse.query.get_or_404(warehouse_id)
    wh.is_active = not wh.is_active
    db.session.commit()
    return redirect(url_for("warehouses.list_warehouses"))


@bp.route("/<int:warehouse_id>/cells")
def cells(warehouse_id):
    wh = Warehouse.query.get_or_404(warehouse_id)
    cell_list = Cell.query.filter_by(warehouse_id=wh.id).order_by(Cell.code).all()
    zone_list = Zone.query.filter_by(warehouse_id=wh.id).order_by(Zone.code).all()
    return render_template(
        "warehouses/cells.html", warehouse=wh, cells=cell_list, zones=zone_list
    )


@bp.route("/<int:warehouse_id>/zones/create", methods=["POST"])
def create_zone(warehouse_id):
    wh = Warehouse.query.get_or_404(warehouse_id)
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()

    if not code:
        flash("Укажите код зоны", "danger")
        return redirect(url_for("warehouses.cells", warehouse_id=wh.id))

    if Zone.query.filter_by(warehouse_id=wh.id, code=code).first():
        flash(f"Зона '{code}' уже существует на этом складе", "danger")
        return redirect(url_for("warehouses.cells", warehouse_id=wh.id))

    zone = Zone(warehouse_id=wh.id, code=code, name=name)
    db.session.add(zone)
    db.session.commit()
    flash(f"Зона '{code}' создана", "success")
    return redirect(url_for("warehouses.cells", warehouse_id=wh.id))


@bp.route("/zones/<int:zone_id>/toggle", methods=["POST"])
def toggle_zone(zone_id):
    zone = Zone.query.get_or_404(zone_id)
    zone.is_active = not zone.is_active
    db.session.commit()
    return redirect(url_for("warehouses.cells", warehouse_id=zone.warehouse_id))


@bp.route("/<int:warehouse_id>/cells/create", methods=["POST"])
def create_cell(warehouse_id):
    wh = Warehouse.query.get_or_404(warehouse_id)
    code = request.form.get("code", "").strip()
    description = request.form.get("description", "").strip()
    zone_id = request.form.get("zone_id", type=int) or None

    if not code:
        flash("Укажите код ячейки", "danger")
        return redirect(url_for("warehouses.cells", warehouse_id=wh.id))

    if Cell.query.filter_by(warehouse_id=wh.id, code=code).first():
        flash(f"Ячейка '{code}' уже существует на этом складе", "danger")
        return redirect(url_for("warehouses.cells", warehouse_id=wh.id))

    if zone_id and not Zone.query.filter_by(id=zone_id, warehouse_id=wh.id).first():
        flash("Зона не найдена на этом складе", "danger")
        return redirect(url_for("warehouses.cells", warehouse_id=wh.id))

    cell = Cell(warehouse_id=wh.id, code=code, description=description, zone_id=zone_id)
    db.session.add(cell)
    db.session.commit()
    flash(f"Ячейка '{code}' создана", "success")
    return redirect(url_for("warehouses.cells", warehouse_id=wh.id))


@bp.route("/cells/<int:cell_id>/toggle", methods=["POST"])
def toggle_cell(cell_id):
    cell = Cell.query.get_or_404(cell_id)
    cell.is_active = not cell.is_active
    db.session.commit()
    return redirect(url_for("warehouses.cells", warehouse_id=cell.warehouse_id))
