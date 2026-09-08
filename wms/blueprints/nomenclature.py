from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import (
    Box,
    BoxItem,
    InventoryLine,
    Nomenclature,
    PlacementLine,
    ProductCategory,
    ProductionRecord,
    ReceivingLine,
    ShipmentPlanLine,
    UnplacedStock,
    Warehouse,
)
from ..utils.categorize import classify_by_name
from ..utils.excel_io import (
    build_nomenclature_template,
    export_nomenclature_to_excel,
    import_nomenclature_from_excel,
    timestamp_for_filename,
)
from ..utils.http import content_disposition

bp = Blueprint("nomenclature", __name__)


@bp.route("/clear", methods=["POST"])
def clear_nomenclature():
    """Удаляет из номенклатуры все позиции, которые нигде не использовались
    (нет остатков, нет строк ни в одном документе/движении) — например,
    чтобы стереть пробный/ошибочный импорт перед чистой загрузкой. Товары,
    хоть раз засветившиеся в реальных данных, не трогаем — иначе документы
    и остатки, которые на них ссылаются, осиротеют."""
    if not current_user.is_admin:
        flash("Очищать номенклатуру может только администратор", "danger")
        return redirect(url_for("nomenclature.list_nomenclature"))

    referenced_ids = set()
    for column in (
        BoxItem.nomenclature_id,
        ReceivingLine.nomenclature_id,
        PlacementLine.nomenclature_id,
        InventoryLine.nomenclature_id,
        ProductionRecord.nomenclature_id,
        ShipmentPlanLine.nomenclature_id,
        UnplacedStock.nomenclature_id,
    ):
        referenced_ids.update(row[0] for row in db.session.query(column).distinct().all())

    query = Nomenclature.query
    if referenced_ids:
        query = query.filter(~Nomenclature.id.in_(referenced_ids))
    candidates = query.all()

    total = Nomenclature.query.count()
    deleted = len(candidates)
    for item in candidates:
        db.session.delete(item)
    db.session.commit()

    flash(
        f"Удалено товаров без истории: {deleted}. "
        f"Оставлено (есть остатки/документы): {total - deleted}",
        "success",
    )
    return redirect(url_for("nomenclature.list_nomenclature"))


@bp.route("/locate")
def locate():
    """Поиск товара по штрихкоду: где он сейчас физически лежит — по
    складам/ячейкам/коробам (упакован) и отдельно неразмещенный остаток
    (принят, но еще не упакован в короб)."""
    barcode = request.args.get("barcode", "").strip()
    item = None
    box_rows = []
    unplaced_rows = []
    not_found = False

    if barcode:
        item = Nomenclature.query.filter_by(barcode=barcode).first()
        if item is None:
            not_found = True
        else:
            box_rows = (
                BoxItem.query.filter_by(nomenclature_id=item.id)
                .join(Box)
                .join(Warehouse, Box.warehouse_id == Warehouse.id)
                .order_by(Warehouse.code, Box.box_number)
                .all()
            )
            unplaced_rows = (
                UnplacedStock.query.filter_by(nomenclature_id=item.id)
                .filter(UnplacedStock.qty > 0)
                .join(Warehouse)
                .order_by(Warehouse.code)
                .all()
            )

    return render_template(
        "nomenclature/locate.html",
        barcode=barcode,
        item=item,
        box_rows=box_rows,
        unplaced_rows=unplaced_rows,
        not_found=not_found,
    )


@bp.route("/")
def list_nomenclature():
    q = request.args.get("q", "").strip()
    query = Nomenclature.query
    if q:
        # Каждое слово запроса ищем отдельно (в любом порядке) — так
        # "кар беж" находит "Кардиган бежевый 44-45".
        for token in q.split():
            like = f"%{token}%"
            query = query.filter(
                db.or_(
                    Nomenclature.name.ilike(like),
                    Nomenclature.sku.ilike(like),
                    Nomenclature.barcode.ilike(like),
                )
            )
    items = query.order_by(Nomenclature.name).all()
    categories = ProductCategory.query.order_by(ProductCategory.name).all()
    return render_template("nomenclature/list.html", items=items, q=q, categories=categories)


@bp.route("/create", methods=["POST"])
def create_nomenclature():
    barcode = request.form.get("barcode", "").strip()
    name = request.form.get("name", "").strip()
    size = request.form.get("size", "").strip()
    sku = request.form.get("sku", "").strip()
    unit = request.form.get("unit", "шт").strip() or "шт"
    description = request.form.get("description", "").strip()
    norm_minutes = request.form.get("norm_minutes", type=float)

    if not barcode or not name:
        flash("Укажите штрихкод и наименование", "danger")
        return redirect(url_for("nomenclature.list_nomenclature"))

    if not sku:
        sku = barcode

    if Nomenclature.query.filter_by(barcode=barcode).first():
        flash(f"Штрихкод '{barcode}' уже используется", "danger")
        return redirect(url_for("nomenclature.list_nomenclature"))

    if Nomenclature.query.filter_by(sku=sku).first():
        flash(f"Товар с артикулом '{sku}' уже существует", "danger")
        return redirect(url_for("nomenclature.list_nomenclature"))

    category = classify_by_name(name)

    item = Nomenclature(
        sku=sku,
        barcode=barcode,
        name=name,
        size=size or None,
        unit=unit,
        description=description,
        norm_minutes=norm_minutes,
        category_id=category.id if category else None,
    )
    db.session.add(item)
    db.session.commit()
    flash(f"Товар '{name}' добавлен", "success")
    return redirect(url_for("nomenclature.list_nomenclature"))


@bp.route("/<int:item_id>/norm", methods=["POST"])
def update_norm(item_id):
    """Норма времени на 1 шт для расчета эффективности в модуле «Производство»."""
    item = Nomenclature.query.get_or_404(item_id)
    norm_minutes = request.form.get("norm_minutes", type=float)
    item.norm_minutes = norm_minutes
    db.session.commit()
    flash(f"Норма для «{item.name}» обновлена", "success")
    return redirect(url_for("nomenclature.list_nomenclature", q=request.form.get("q", "")))


@bp.route("/<int:item_id>/category", methods=["POST"])
def update_category(item_id):
    """Вид товара определяется автоматически по названию при создании, но
    его можно поправить вручную (например, если название нетипичное)."""
    item = Nomenclature.query.get_or_404(item_id)
    category_id = request.form.get("category_id", type=int)
    item.category_id = category_id or None
    db.session.commit()
    flash(f"Вид товара для «{item.name}» обновлен", "success")
    return redirect(url_for("nomenclature.list_nomenclature", q=request.form.get("q", "")))


@bp.route("/template.xlsx")
def download_template():
    data = build_nomenclature_template()
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=nomenclature_template.xlsx"},
    )


@bp.route("/export.xlsx")
def export_all():
    items = Nomenclature.query.order_by(Nomenclature.name).all()
    data = export_nomenclature_to_excel(items)
    fname = f"nomenclature_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


@bp.route("/import", methods=["GET", "POST"])
def import_nomenclature():
    if request.method == "GET":
        return render_template("nomenclature/import.html")

    file = request.files.get("file")
    if not file or file.filename == "":
        flash("Выберите файл xlsx для импорта", "danger")
        return redirect(url_for("nomenclature.import_nomenclature"))

    result = import_nomenclature_from_excel(file.stream, db, Nomenclature)
    db.session.commit()

    flash(
        f"Импорт завершен: создано {result.created}, обновлено {result.updated}, "
        f"ошибок {len(result.errors)}",
        "success" if not result.errors else "warning",
    )
    return render_template("nomenclature/import.html", result=result)
