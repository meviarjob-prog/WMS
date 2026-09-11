from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import Box, BoxItem, Nomenclature, Warehouse
from ..utils.numbering import next_number

bp = Blueprint("boxes", __name__)


@bp.route("/")
def list_boxes():
    """Общий список коробов — нужен в первую очередь чтобы допечатать
    этикетки позже: если при массовой печати партии физически не хватило
    этикеток (закончилась лента/пачка стикеров), короба в системе уже
    созданы все разом, а часть из них останется без наклейки. Здесь можно
    в любой момент найти нужные короба (по складу/номеру) и распечатать
    только оставшиеся, не создавая короба заново."""
    warehouses = Warehouse.query.order_by(Warehouse.code).all()

    warehouse_id = request.args.get("warehouse_id", type=int)
    query_text = request.args.get("q", "").strip()

    query = Box.query
    if warehouse_id:
        query = query.filter_by(warehouse_id=warehouse_id)
    if query_text:
        query = query.filter(Box.box_number.ilike(f"%{query_text}%"))

    boxes = query.order_by(Box.box_number.desc()).limit(500).all()

    # Кол-во позиций в коробе — одним групповым запросом на все короба
    # страницы, а не box.items.count() в цикле шаблона на каждую строку.
    box_ids = [box.id for box in boxes]
    item_counts = dict(
        db.session.query(BoxItem.box_id, db.func.count(BoxItem.id))
        .filter(BoxItem.box_id.in_(box_ids))
        .group_by(BoxItem.box_id)
        .all()
    ) if box_ids else {}

    return render_template(
        "boxes/list.html",
        boxes=boxes,
        warehouses=warehouses,
        warehouse_id=warehouse_id,
        query_text=query_text,
        item_counts=item_counts,
    )


@bp.route("/<int:box_id>")
def detail(box_id):
    box = Box.query.get_or_404(box_id)
    items = box.items.all()
    return render_template("boxes/detail.html", box=box, items=items)


@bp.route("/<int:box_id>/items/add", methods=["POST"])
def add_item(box_id):
    """Ручная корректировка состава короба администратором — напрямую, в
    обход документов приемки/размещения/перемещения. Нужна, когда факт не
    совпал с тем, что отсканировали (не туда положили, ошиблись коробом и
    т.п.), а документ, которым товар туда попал, уже завершен и его
    позиции больше не редактируются."""
    if not current_user.is_admin:
        flash("Редактировать состав короба может только администратор", "danger")
        return redirect(url_for("boxes.detail", box_id=box_id))

    box = Box.query.get_or_404(box_id)
    nomenclature_id = request.form.get("nomenclature_id", type=int)
    qty = request.form.get("qty", type=float)
    item = Nomenclature.query.get(nomenclature_id)
    if not item or not qty or qty <= 0:
        flash("Укажите товар и корректное количество", "danger")
        return redirect(url_for("boxes.detail", box_id=box_id))

    box_item = BoxItem.query.filter_by(box_id=box.id, nomenclature_id=item.id).first()
    if box_item:
        box_item.qty += qty
    else:
        box_item = BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty)
        db.session.add(box_item)
    db.session.commit()
    flash(f"В короб {box.box_number} добавлено: {item.name} ({qty} {item.unit})", "success")
    return redirect(url_for("boxes.detail", box_id=box_id))


@bp.route("/<int:box_id>/items/<int:item_id>/update", methods=["POST"])
def update_item(box_id, item_id):
    if not current_user.is_admin:
        flash("Редактировать состав короба может только администратор", "danger")
        return redirect(url_for("boxes.detail", box_id=box_id))

    box_item = BoxItem.query.filter_by(id=item_id, box_id=box_id).first_or_404()
    qty = request.form.get("qty", type=float)
    if qty is None or qty <= 0:
        flash("Укажите корректное количество", "danger")
        return redirect(url_for("boxes.detail", box_id=box_id))

    box_item.qty = qty
    db.session.commit()
    flash(f"Количество обновлено: {box_item.nomenclature.name} — {qty} {box_item.nomenclature.unit}", "success")
    return redirect(url_for("boxes.detail", box_id=box_id))


@bp.route("/<int:box_id>/items/<int:item_id>/move", methods=["POST"])
def move_item(box_id, item_id):
    """Перенос товара из этого короба в другой — например, при перепаковке
    или если по факту положили не в тот короб. Ищем целевой короб только на
    этом же складе (короб привязан к складу физически, перенос между
    складами — это уже «Перемещение», а не правка состава короба)."""
    if not current_user.is_admin:
        flash("Переносить товар между коробами может только администратор", "danger")
        return redirect(url_for("boxes.detail", box_id=box_id))

    box_item = BoxItem.query.filter_by(id=item_id, box_id=box_id).first_or_404()
    box = box_item.box

    target_box_number = request.form.get("target_box_number", "").strip()
    qty = request.form.get("qty", type=float)

    if not qty or qty <= 0 or qty > box_item.qty:
        flash(
            f"Укажите корректное количество для переноса (доступно {box_item.qty:g})",
            "danger",
        )
        return redirect(url_for("boxes.detail", box_id=box_id))

    target_box = Box.find_by_scanned_code(target_box_number, warehouse_id=box.warehouse_id)
    if not target_box:
        flash(
            f"Короб '{target_box_number}' не найден на складе «{box.warehouse.name}»",
            "danger",
        )
        return redirect(url_for("boxes.detail", box_id=box_id))

    if target_box.id == box.id:
        flash("Целевой короб совпадает с исходным — переносить некуда", "danger")
        return redirect(url_for("boxes.detail", box_id=box_id))

    item = box_item.nomenclature
    box_item.qty -= qty
    if box_item.qty <= 0:
        db.session.delete(box_item)

    target_item = BoxItem.query.filter_by(box_id=target_box.id, nomenclature_id=item.id).first()
    if target_item:
        target_item.qty += qty
    else:
        target_item = BoxItem(box_id=target_box.id, nomenclature_id=item.id, qty=qty)
        db.session.add(target_item)

    db.session.commit()
    flash(
        f"Перенесено в короб {target_box.box_number}: {item.name} ({qty:g} {item.unit})",
        "success",
    )
    return redirect(url_for("boxes.detail", box_id=box_id))


@bp.route("/<int:box_id>/items/<int:item_id>/delete", methods=["POST"])
def delete_item(box_id, item_id):
    if not current_user.is_admin:
        flash("Редактировать состав короба может только администратор", "danger")
        return redirect(url_for("boxes.detail", box_id=box_id))

    box_item = BoxItem.query.filter_by(id=item_id, box_id=box_id).first_or_404()
    name = box_item.nomenclature.name
    db.session.delete(box_item)
    db.session.commit()
    flash(f"Из короба удалено: {name}", "success")
    return redirect(url_for("boxes.detail", box_id=box_id))


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
