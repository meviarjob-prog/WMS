from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..extensions import db
from ..models import Cell, Warehouse

bp = Blueprint("warehouses", __name__)


@bp.route("/")
def list_warehouses():
    warehouses = Warehouse.query.order_by(Warehouse.code).all()
    return render_template("warehouses/list.html", warehouses=warehouses)


@bp.route("/create", methods=["POST"])
def create_warehouse():
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    address = request.form.get("address", "").strip()

    if not code or not name:
        flash("Укажите код и наименование склада", "danger")
        return redirect(url_for("warehouses.list_warehouses"))

    if Warehouse.query.filter_by(code=code).first():
        flash(f"Склад с кодом '{code}' уже существует", "danger")
        return redirect(url_for("warehouses.list_warehouses"))

    wh = Warehouse(code=code, name=name, address=address)
    db.session.add(wh)
    db.session.commit()
    flash(f"Склад '{name}' создан", "success")
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
    return render_template("warehouses/cells.html", warehouse=wh, cells=cell_list)


@bp.route("/<int:warehouse_id>/cells/create", methods=["POST"])
def create_cell(warehouse_id):
    wh = Warehouse.query.get_or_404(warehouse_id)
    code = request.form.get("code", "").strip()
    description = request.form.get("description", "").strip()

    if not code:
        flash("Укажите код ячейки", "danger")
        return redirect(url_for("warehouses.cells", warehouse_id=wh.id))

    if Cell.query.filter_by(warehouse_id=wh.id, code=code).first():
        flash(f"Ячейка '{code}' уже существует на этом складе", "danger")
        return redirect(url_for("warehouses.cells", warehouse_id=wh.id))

    cell = Cell(warehouse_id=wh.id, code=code, description=description)
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
