from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..extensions import db
from ..models import Box, Cell, ShipmentPlanLine, Warehouse, Zone
from ..utils.numbering import next_number

bp = Blueprint("warehouses", __name__)

# Ширина числовой части кода ячейки: код ряда "A" -> ячейки "A0001", "A0002", ...
# Ряд остается моделью Zone в БД (менять таблицу/класс ради переименования
# в интерфейсе избыточно) — но по смыслу и в тексте для пользователя это
# именно "ряд", а не свободно называемая "зона".
CELL_NUMBER_WIDTH = 4


def _generate_cells(zone, count):
    """Создает `count` новых ячеек в ряду с кодами вида "<код ряда><NNNN>",
    продолжая нумерацию с того места, где она остановилась в этом ряду —
    чтобы короб можно было сразу и однозначно сканировать в ячейку при
    размещении, без придумывания кода вручную."""
    created = []
    for _ in range(count):
        code = next_number(
            f"row_cells:{zone.id}", prefix=zone.code, width=CELL_NUMBER_WIDTH
        )
        cell = Cell(warehouse_id=zone.warehouse_id, zone_id=zone.id, code=code)
        db.session.add(cell)
        created.append(cell)
    return created


@bp.route("/")
def list_warehouses():
    # Склады-города (marketplace задан) создаются автоматически при загрузке
    # плана отгрузок и не удаляются, когда город пропадает из очередной
    # выгрузки (plan.lines.delete() чистит только строки, не сами склады) —
    # поэтому список показываем полностью, но помечаем у каждого
    # склада-города, есть ли он в текущем плане, чтобы можно было
    # ориентироваться и вручную отключить неактуальные.
    active_city_warehouse_ids = {
        row[0] for row in db.session.query(ShipmentPlanLine.warehouse_id).distinct()
    }
    warehouses = Warehouse.query.order_by(Warehouse.code).all()
    for wh in warehouses:
        wh.in_current_plan = wh.marketplace is None or wh.id in active_city_warehouse_ids
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
    """Создает ряд склада и сразу же генерирует под него ячейки (если
    указано их количество) — коды ячеек присваиваются автоматически
    ("<ряд><NNNN>"), вручную придумывать и вводить их не нужно."""
    wh = Warehouse.query.get_or_404(warehouse_id)
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    cell_count = request.form.get("cell_count", type=int) or 0

    if not code:
        flash("Укажите название ряда", "danger")
        return redirect(url_for("warehouses.cells", warehouse_id=wh.id))

    if Zone.query.filter_by(warehouse_id=wh.id, code=code).first():
        flash(f"Ряд '{code}' уже существует на этом складе", "danger")
        return redirect(url_for("warehouses.cells", warehouse_id=wh.id))

    zone = Zone(warehouse_id=wh.id, code=code, name=name)
    db.session.add(zone)
    db.session.flush()

    message = f"Ряд '{code}' создан"
    if cell_count > 0:
        cells = _generate_cells(zone, cell_count)
        message += f", ячеек создано: {len(cells)} ({cells[0].code}–{cells[-1].code})"

    db.session.commit()
    flash(message, "success")
    return redirect(url_for("warehouses.cells", warehouse_id=wh.id))


@bp.route("/zones/<int:zone_id>/toggle", methods=["POST"])
def toggle_zone(zone_id):
    zone = Zone.query.get_or_404(zone_id)
    zone.is_active = not zone.is_active
    db.session.commit()
    return redirect(url_for("warehouses.cells", warehouse_id=zone.warehouse_id))


@bp.route("/zones/<int:zone_id>/cells/add", methods=["POST"])
def add_cells(zone_id):
    """Догенерировать еще ячеек в существующем ряду — нумерация продолжается
    с того места, на котором остановилась в этом ряду."""
    zone = Zone.query.get_or_404(zone_id)
    count = request.form.get("count", type=int) or 0

    if count <= 0:
        flash("Укажите количество ячеек", "danger")
        return redirect(url_for("warehouses.cells", warehouse_id=zone.warehouse_id))

    cells = _generate_cells(zone, count)
    db.session.commit()
    flash(f"В ряду '{zone.code}' создано ячеек: {len(cells)} ({cells[0].code}–{cells[-1].code})", "success")
    return redirect(url_for("warehouses.cells", warehouse_id=zone.warehouse_id))


@bp.route("/cells/<int:cell_id>/toggle", methods=["POST"])
def toggle_cell(cell_id):
    cell = Cell.query.get_or_404(cell_id)
    cell.is_active = not cell.is_active
    db.session.commit()
    return redirect(url_for("warehouses.cells", warehouse_id=cell.warehouse_id))


@bp.route("/cells/<int:cell_id>")
def cell_detail(cell_id):
    cell = Cell.query.get_or_404(cell_id)
    boxes = Box.query.filter_by(cell_id=cell.id).order_by(Box.box_number).all()
    return render_template("warehouses/cell_detail.html", cell=cell, boxes=boxes)
