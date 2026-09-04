from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..extensions import db
from ..models import Box, Warehouse
from ..utils.numbering import next_number

bp = Blueprint("boxes", __name__)


@bp.route("/<int:box_id>")
def detail(box_id):
    box = Box.query.get_or_404(box_id)
    items = box.items.all()
    return render_template("boxes/detail.html", box=box, items=items)


@bp.route("/bulk-create", methods=["GET", "POST"])
def bulk_create():
    """Заготовить сразу партию пустых коробов под один склад — чтобы сразу
    распечатать все этикетки одним разом ("первая проклейка"), а сами
    короба заполнять товаром позже, по мере надобности (в «Размещении»
    любой такой короб доступен для упаковки или можно сразу поставить
    пустым в ячейку/зону)."""
    if request.method == "GET":
        warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.code).all()
        return render_template("boxes/bulk_create.html", warehouses=warehouses)

    warehouse_id = request.form.get("warehouse_id", type=int)
    count = request.form.get("count", type=int)

    if not warehouse_id:
        flash("Выберите склад", "danger")
        return redirect(url_for("boxes.bulk_create"))

    if not count or count < 1 or count > 500:
        flash("Укажите количество коробов от 1 до 500", "danger")
        return redirect(url_for("boxes.bulk_create"))

    created = []
    for _ in range(count):
        box = Box(box_number=next_number("box"), warehouse_id=warehouse_id, status="open")
        db.session.add(box)
        created.append(box)
    db.session.commit()

    flash(f"Создано коробов: {len(created)}", "success")
    return render_template("boxes/bulk_result.html", boxes=created)
