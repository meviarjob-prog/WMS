from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import (
    Box,
    BoxItem,
    InventoryLine,
    MovementReceiptDiscrepancy,
    Nomenclature,
    PHYSICAL_STOCK_WAREHOUSE_NAMES,
    PlacementLine,
    ProductCategory,
    ProductionRecord,
    ReceivingLine,
    ShipmentPlanLine,
    SupplierReturn,
    UnplacedStock,
    UnplacedStockLot,
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


def _require_edit():
    """Просмотр номенклатуры и раздела доступен всем с доступом к разделу
    (see SECTIONS) — а вот менять её (добавлять/править вид и норму/
    импортировать) можно только с отдельным правом (см.
    User.nomenclature_edit_allowed), которое настраивается отдельно от
    доступа к разделу в «Настройки» → «Разделы»."""
    if current_user.can_edit_nomenclature():
        return True
    flash("Редактировать номенклатуру вам не разрешено — обратитесь к администратору", "danger")
    return False


def _referenced_nomenclature_ids():
    """ID товаров, на которые где-либо ссылаются реальные данные (остатки,
    строки документов/движений) — такие удалять нельзя, иначе эти записи
    осиротеют. Используется и при массовой очистке (clear_nomenclature), и
    при удалении одного товара (delete_nomenclature)."""
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
    return referenced_ids


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

    referenced_ids = _referenced_nomenclature_ids()

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


# Таблицы, где ограничение уникальности включает nomenclature_id — просто
# перевесить ссылку на объединенный товар нельзя, если у него уже есть своя
# строка с тем же вторым ключом (см. _merge_nomenclature): такую строку
# нужно слить (сложить qty), а не задвоить нарушением constraint.
_MERGE_UNIQUE_KEY_BY_MODEL = {
    UnplacedStock: "warehouse_id",
    InventoryLine: "document_id",
}
# Остальные таблицы со ссылкой на номенклатуру — ограничений уникальности
# по нomenclature_id нет, просто перевешиваем ссылку. Сводные отчеты и так
# считают суммы через SUM/GROUP BY nomenclature_id, задвоение строк внутри
# одного документа на суммы не влияет.
_MERGE_SIMPLE_MODELS = (
    BoxItem, UnplacedStockLot, ReceivingLine, PlacementLine,
    MovementReceiptDiscrepancy, ProductionRecord, ShipmentPlanLine, SupplierReturn,
)


def _duplicate_name_groups():
    """Группирует номенклатуру по совпадающему наименованию (без учета
    регистра и краевых пробелов) — каждая группа из 2+ товаров возможный
    кандидат на объединение (см. чат). Не объединяем автоматически: разные
    размеры/цвета иногда имеют одинаковое "Наименование" в исходном файле и
    это НЕ дубли — решение всегда принимает человек на странице /duplicates,
    видя штрихкод/артикул/размер/остаток каждого кандидата."""
    key = db.func.lower(db.func.trim(Nomenclature.name))
    names_with_dupes = (
        db.session.query(key)
        .group_by(key)
        .having(db.func.count(Nomenclature.id) > 1)
        .all()
    )
    if not names_with_dupes:
        return []

    stock_by_item = {}
    for nomenclature_id, qty in (
        db.session.query(UnplacedStock.nomenclature_id, db.func.sum(UnplacedStock.qty))
        .group_by(UnplacedStock.nomenclature_id)
        .all()
    ):
        stock_by_item[nomenclature_id] = stock_by_item.get(nomenclature_id, 0) + (qty or 0)
    for nomenclature_id, qty in (
        db.session.query(BoxItem.nomenclature_id, db.func.sum(BoxItem.qty))
        .group_by(BoxItem.nomenclature_id)
        .all()
    ):
        stock_by_item[nomenclature_id] = stock_by_item.get(nomenclature_id, 0) + (qty or 0)

    groups = []
    for (normalized_name,) in names_with_dupes:
        items = (
            Nomenclature.query.filter(db.func.lower(db.func.trim(Nomenclature.name)) == normalized_name)
            .order_by(Nomenclature.id)
            .all()
        )
        groups.append(
            {
                "name": items[0].name,
                "rows": [
                    {"item": item, "stock": stock_by_item.get(item.id, 0)} for item in items
                ],
            }
        )
    groups.sort(key=lambda g: g["name"])
    return groups


@bp.route("/duplicates")
def duplicates():
    if not _require_edit():
        return redirect(url_for("nomenclature.list_nomenclature"))
    return render_template("nomenclature/duplicates.html", groups=_duplicate_name_groups())


def _merge_nomenclature(primary, duplicate):
    """Переносит все ссылки на duplicate на primary и удаляет duplicate.
    Обходит ВСЕ таблицы со ссылкой на номенклатуру (не только те, что
    считаются "историей" в _referenced_nomenclature_ids) — иначе документы,
    которые не входят в этот список (например, SupplierReturn), остались бы
    ссылаться на удаленную строку."""
    for model, key_field in _MERGE_UNIQUE_KEY_BY_MODEL.items():
        for dup_row in model.query.filter_by(nomenclature_id=duplicate.id).all():
            key_value = getattr(dup_row, key_field)
            primary_row = model.query.filter_by(
                nomenclature_id=primary.id, **{key_field: key_value}
            ).first()
            if primary_row:
                primary_row.qty += dup_row.qty
                db.session.delete(dup_row)
            else:
                dup_row.nomenclature_id = primary.id

    for model in _MERGE_SIMPLE_MODELS:
        for row in model.query.filter_by(nomenclature_id=duplicate.id).all():
            row.nomenclature_id = primary.id

    # Доп. штрихкод объединяемого товара не теряем — иначе короб/этикетка со
    # старым штрихкодом дубля перестанет находиться после слияния. Если у
    # primary доп. штрихкод уже занят другим кодом, штрихкоды дубля просто
    # пропадают (крайний случай — у товара может быть только один доп.
    # штрихкод, см. чат).
    if primary.barcode2 is None:
        primary.barcode2 = duplicate.barcode or duplicate.barcode2

    db.session.delete(duplicate)


@bp.route("/duplicates/merge", methods=["POST"])
def merge_duplicates():
    """Объединяет выбранные позиции внутри одной группы дублей в одну — все
    остатки/строки документов переносятся на оставленный товар (см.
    _merge_nomenclature), дубли удаляются. Необратимо, поэтому только
    администратор — как и остальные удаляющие действия в номенклатуре
    (delete_nomenclature/clear_nomenclature)."""
    if not current_user.is_admin:
        flash("Объединять дубли может только администратор", "danger")
        return redirect(url_for("nomenclature.duplicates"))

    keep_id = request.form.get("keep_id", type=int)
    merge_ids = [i for i in request.form.getlist("merge_ids", type=int) if i != keep_id]
    primary = Nomenclature.query.get(keep_id) if keep_id else None
    if not primary or not merge_ids:
        flash("Выберите товар для объединения и хотя бы один дубль", "danger")
        return redirect(url_for("nomenclature.duplicates"))

    duplicates_qs = Nomenclature.query.filter(Nomenclature.id.in_(merge_ids)).all()
    if len(duplicates_qs) != len(merge_ids):
        flash("Не удалось найти все выбранные товары — возможно, список уже изменился", "danger")
        return redirect(url_for("nomenclature.duplicates"))

    merged_names = ", ".join(d.name for d in duplicates_qs)
    for duplicate in duplicates_qs:
        _merge_nomenclature(primary, duplicate)
    db.session.commit()

    flash(f"«{primary.name}» (id {primary.id}) объединен с: {merged_names}. Дубли удалены.", "success")
    return redirect(url_for("nomenclature.duplicates"))


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
        item = Nomenclature.find_by_barcode(barcode)
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
            # Откуда взялся неразмещенный остаток: партии по приемкам; что
            # не покрыто партиями — излишек приемки перемещения/старые данные.
            lots_by_warehouse = {}
            for lot in (
                UnplacedStockLot.query.filter_by(nomenclature_id=item.id)
                .filter(UnplacedStockLot.qty_remaining > 0)
                .order_by(UnplacedStockLot.received_at)
                .all()
            ):
                lots_by_warehouse.setdefault(lot.warehouse_id, []).append(lot)
            for row in unplaced_rows:
                row.source_lots = lots_by_warehouse.get(row.warehouse_id, [])
                row.unexplained_qty = max(row.qty - sum(l.qty_remaining for l in row.source_lots), 0)

    return render_template(
        "nomenclature/locate.html",
        barcode=barcode,
        item=item,
        box_rows=box_rows,
        unplaced_rows=unplaced_rows,
        not_found=not_found,
    )


@bp.route("/locate/unplaced/<int:stock_id>/set", methods=["POST"])
def unplaced_set(stock_id):
    """Администратор приводит неразмещенный остаток склада к факту — для
    исправления задвоений (товар уже лежит в коробах, а остаток числится
    еще и неразмещенным). Только уменьшение: лишнее списывается по партиям
    (FIFO), как при размещении."""
    row = UnplacedStock.query.get_or_404(stock_id)
    barcode = row.nomenclature.barcode
    if not current_user.is_admin:
        flash("Списывать остаток может только администратор", "danger")
        return redirect(url_for("nomenclature.locate", barcode=barcode))
    qty = request.form.get("qty", type=float)
    if qty is None or qty < 0 or qty >= row.qty:
        flash(f"Укажите фактический остаток от 0 до {row.qty:g}", "danger")
        return redirect(url_for("nomenclature.locate", barcode=barcode))
    write_off = row.qty - qty
    UnplacedStock.consume(row.warehouse_id, row.nomenclature_id, write_off)
    db.session.commit()
    current_app.logger.warning(
        "Неразмещенный остаток списан: %s, склад %s, %g -> %g (пользователь %s)",
        row.nomenclature.name, row.warehouse.name, qty + write_off, qty, current_user.username,
    )
    flash(f"Неразмещенный остаток «{row.nomenclature.name}» на складе «{row.warehouse.name}»: списано {write_off:g}, осталось {qty:g}", "success")
    return redirect(url_for("nomenclature.locate", barcode=barcode))


NOMENCLATURE_PAGE_SIZE = 100


# Только согласованные физические склады (см. PHYSICAL_STOCK_WAREHOUSE_NAMES
# в models.py). Остальные склады в системе — города маркетплейсов (Ozon/WB),
# это уже конкретная отгрузка, а не остаток "сколько у нас есть на складе"
# (см. shipment_plan.py, где остаток по ним считается отдельно и иначе).
def _stock_warehouses():
    return (
        Warehouse.query.filter(
            Warehouse.is_active.is_(True),
            db.func.lower(db.func.trim(Warehouse.name)).in_(PHYSICAL_STOCK_WAREHOUSE_NAMES),
        )
        .order_by(Warehouse.code)
        .all()
    )


def _stock_by_item_and_warehouse(item_ids=None, warehouse_ids=None):
    """{(nomenclature_id, warehouse_id): кол-во} — сумма упакованного в
    короба и неразмещенного остатка, разложенная по складу. item_ids=None —
    по всему каталогу сразу (см. export_all), иначе только по перечисленным
    (список/страница — не тянуть остаток по всему каталогу лишний раз).
    warehouse_ids=None — по всем складам без ограничения; для остатка в
    номенклатуре и его выгрузки вызывающий код сам передает id складов из
    _stock_warehouses(), чтобы не смешивать физический остаток с городами
    маркетплейсов."""
    result = {}
    box_query = (
        db.session.query(BoxItem.nomenclature_id, Box.warehouse_id, db.func.sum(BoxItem.qty))
        .join(Box, BoxItem.box_id == Box.id)
    )
    unplaced_query = db.session.query(
        UnplacedStock.nomenclature_id, UnplacedStock.warehouse_id, db.func.sum(UnplacedStock.qty)
    ).filter(UnplacedStock.qty > 0)
    if item_ids is not None:
        box_query = box_query.filter(BoxItem.nomenclature_id.in_(item_ids))
        unplaced_query = unplaced_query.filter(UnplacedStock.nomenclature_id.in_(item_ids))
    if warehouse_ids is not None:
        box_query = box_query.filter(Box.warehouse_id.in_(warehouse_ids))
        unplaced_query = unplaced_query.filter(UnplacedStock.warehouse_id.in_(warehouse_ids))
    for nid, wid, qty in box_query.group_by(BoxItem.nomenclature_id, Box.warehouse_id).all():
        result[(nid, wid)] = result.get((nid, wid), 0) + qty
    for nid, wid, qty in unplaced_query.group_by(
        UnplacedStock.nomenclature_id, UnplacedStock.warehouse_id
    ).all():
        result[(nid, wid)] = result.get((nid, wid), 0) + qty
    return result


@bp.route("/")
def list_nomenclature():
    """Каталог может разрастись до тысяч позиций (реальный ассортимент
    одежды по артикулам/размерам) — без постраничной разбивки страница
    рендерила все строки разом, а в каждой строке еще и полный select видов
    товара, так что HTML на выходе становился очень тяжелым и открывался
    заметно медленно. Поэтому список постраничный; поиск (q) сбрасывает
    страницу на первую."""
    q = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
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
                    Nomenclature.barcode2.ilike(like),
                )
            )
    pagination = query.order_by(Nomenclature.name).paginate(
        page=page, per_page=NOMENCLATURE_PAGE_SIZE, error_out=False
    )
    categories = ProductCategory.query.order_by(ProductCategory.name).all()

    # Остаток показываем отдельной колонкой на каждый физический склад
    # (Основной, Склад №2 (Шоссейная 167)) — считаем только для позиций
    # текущей страницы, чтобы не тянуть остаток по всему каталогу на
    # каждую загрузку страницы.
    stock_warehouses = _stock_warehouses()
    item_ids = [item.id for item in pagination.items]
    stock_by_item_warehouse = (
        _stock_by_item_and_warehouse(item_ids, [wh.id for wh in stock_warehouses])
        if item_ids
        else {}
    )
    # Неразмещенная часть остатка отдельно — чтобы было видно, сколько в
    # коробах, а сколько числится принятым, но не упакованным.
    unplaced_by_item_warehouse = {}
    if item_ids:
        for nid, wid, qty in (
            db.session.query(UnplacedStock.nomenclature_id, UnplacedStock.warehouse_id, UnplacedStock.qty)
            .filter(
                UnplacedStock.qty > 0,
                UnplacedStock.nomenclature_id.in_(item_ids),
                UnplacedStock.warehouse_id.in_([wh.id for wh in stock_warehouses]),
            )
            .all()
        ):
            unplaced_by_item_warehouse[(nid, wid)] = qty

    return render_template(
        "nomenclature/list.html",
        items=pagination.items,
        pagination=pagination,
        q=q,
        categories=categories,
        stock_warehouses=stock_warehouses,
        stock_by_item_warehouse=stock_by_item_warehouse,
        unplaced_by_item_warehouse=unplaced_by_item_warehouse,
    )


@bp.route("/create", methods=["POST"])
def create_nomenclature():
    if not _require_edit():
        return redirect(url_for("nomenclature.list_nomenclature"))

    barcode = request.form.get("barcode", "").strip()
    barcode2 = request.form.get("barcode2", "").strip()
    name = request.form.get("name", "").strip()
    size = request.form.get("size", "").strip()
    sku = request.form.get("sku", "").strip()
    unit = request.form.get("unit", "шт").strip() or "шт"
    norm_minutes = request.form.get("norm_minutes", type=float)

    if not barcode or not name:
        flash("Укажите штрихкод и наименование", "danger")
        return redirect(url_for("nomenclature.list_nomenclature"))

    if not sku:
        sku = barcode

    if Nomenclature.find_by_barcode(barcode):
        flash(f"Штрихкод '{barcode}' уже используется", "danger")
        return redirect(url_for("nomenclature.list_nomenclature"))

    if barcode2 and Nomenclature.find_by_barcode(barcode2):
        flash(f"Доп. штрихкод '{barcode2}' уже используется", "danger")
        return redirect(url_for("nomenclature.list_nomenclature"))

    if Nomenclature.query.filter_by(sku=sku).first():
        flash(f"Товар с артикулом '{sku}' уже существует", "danger")
        return redirect(url_for("nomenclature.list_nomenclature"))

    category = classify_by_name(name)

    item = Nomenclature(
        sku=sku,
        barcode=barcode,
        barcode2=barcode2 or None,
        name=name,
        size=size or None,
        unit=unit,
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
    if not _require_edit():
        return redirect(url_for("nomenclature.list_nomenclature"))

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
    if not _require_edit():
        return redirect(url_for("nomenclature.list_nomenclature"))

    item = Nomenclature.query.get_or_404(item_id)
    category_id = request.form.get("category_id", type=int)
    item.category_id = category_id or None
    db.session.commit()
    flash(f"Вид товара для «{item.name}» обновлен", "success")
    return redirect(url_for("nomenclature.list_nomenclature", q=request.form.get("q", "")))


@bp.route("/<int:item_id>/barcode", methods=["POST"])
def update_barcode(item_id):
    """Штрихкод иногда нужно поправить прямо в списке — например, если при
    создании товара его ввели с опечаткой или он поменялся у поставщика."""
    if not _require_edit():
        return redirect(url_for("nomenclature.list_nomenclature"))

    item = Nomenclature.query.get_or_404(item_id)
    barcode = request.form.get("barcode", "").strip()
    q = request.form.get("q", "")

    if not barcode:
        flash("Штрихкод не может быть пустым", "danger")
        return redirect(url_for("nomenclature.list_nomenclature", q=q))

    existing = Nomenclature.find_by_barcode(barcode)
    if existing and existing.id != item.id:
        flash(f"Штрихкод '{barcode}' уже используется у товара «{existing.name}»", "danger")
        return redirect(url_for("nomenclature.list_nomenclature", q=q))

    item.barcode = barcode
    db.session.commit()
    flash(f"Штрихкод для «{item.name}» обновлен", "success")
    return redirect(url_for("nomenclature.list_nomenclature", q=q))


@bp.route("/<int:item_id>/barcode2", methods=["POST"])
def update_barcode2(item_id):
    """Доп. штрихкод — второй код, по которому тоже находится этот же товар
    (см. Nomenclature.find_by_barcode и чат: старый штрихкод от поставщика,
    который уже разошелся по коробам/этикеткам, менять сразу на новый
    рискованно). Пустое значение снимает доп. штрихкод."""
    if not _require_edit():
        return redirect(url_for("nomenclature.list_nomenclature"))

    item = Nomenclature.query.get_or_404(item_id)
    barcode2 = request.form.get("barcode2", "").strip()
    q = request.form.get("q", "")

    if barcode2:
        existing = Nomenclature.find_by_barcode(barcode2)
        if existing and existing.id != item.id:
            flash(f"Штрихкод '{barcode2}' уже используется у товара «{existing.name}»", "danger")
            return redirect(url_for("nomenclature.list_nomenclature", q=q))

    item.barcode2 = barcode2 or None
    db.session.commit()
    flash(f"Доп. штрихкод для «{item.name}» обновлен", "success")
    return redirect(url_for("nomenclature.list_nomenclature", q=q))


@bp.route("/<int:item_id>/sku", methods=["POST"])
def update_sku(item_id):
    """Артикул иногда нужно поправить прямо в списке — например, при
    смене нумерации у поставщика или после ручного импорта с опечаткой."""
    if not _require_edit():
        return redirect(url_for("nomenclature.list_nomenclature"))

    item = Nomenclature.query.get_or_404(item_id)
    sku = request.form.get("sku", "").strip()
    q = request.form.get("q", "")

    if not sku:
        flash("Артикул не может быть пустым", "danger")
        return redirect(url_for("nomenclature.list_nomenclature", q=q))

    existing = Nomenclature.query.filter_by(sku=sku).first()
    if existing and existing.id != item.id:
        flash(f"Артикул '{sku}' уже используется у товара «{existing.name}»", "danger")
        return redirect(url_for("nomenclature.list_nomenclature", q=q))

    item.sku = sku
    db.session.commit()
    flash(f"Артикул для «{item.name}» обновлен", "success")
    return redirect(url_for("nomenclature.list_nomenclature", q=q))


@bp.route("/<int:item_id>/name", methods=["POST"])
def update_name(item_id):
    """Наименование иногда нужно поправить прямо в списке — например,
    после ручного импорта с неточным названием."""
    if not _require_edit():
        return redirect(url_for("nomenclature.list_nomenclature"))

    item = Nomenclature.query.get_or_404(item_id)
    name = request.form.get("name", "").strip()
    q = request.form.get("q", "")

    if not name:
        flash("Наименование не может быть пустым", "danger")
        return redirect(url_for("nomenclature.list_nomenclature", q=q))

    item.name = name
    db.session.commit()
    flash(f"Наименование обновлено на «{name}»", "success")
    return redirect(url_for("nomenclature.list_nomenclature", q=q))


@bp.route("/<int:item_id>/delete", methods=["POST"])
def delete_nomenclature(item_id):
    """Удаление одного товара — доступно только администратору. Как и
    массовая очистка (clear_nomenclature), не удаляет товар, если на него
    где-либо ссылаются реальные данные (остатки, строки документов) —
    иначе эти записи осиротеют."""
    q = request.form.get("q", "")
    if not current_user.is_admin:
        flash("Удалять товар может только администратор", "danger")
        return redirect(url_for("nomenclature.list_nomenclature", q=q))

    item = Nomenclature.query.get_or_404(item_id)
    if item.id in _referenced_nomenclature_ids():
        flash(
            f"Нельзя удалить «{item.name}» — по нему есть остатки или документы",
            "danger",
        )
        return redirect(url_for("nomenclature.list_nomenclature", q=q))

    name = item.name
    db.session.delete(item)
    db.session.commit()
    flash(f"Товар «{name}» удален", "success")
    return redirect(url_for("nomenclature.list_nomenclature", q=q))


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
    """?warehouse_id= — считать остаток только по этому складу (должен
    быть одним из _stock_warehouses()); без параметра — суммарно по
    Основному и Складу №2 (Шоссейная 167) вместе, как и в списке."""
    items = Nomenclature.query.order_by(Nomenclature.name).all()
    stock_warehouses = _stock_warehouses()
    warehouse_id = request.args.get("warehouse_id", type=int)
    selected_ids = (
        [warehouse_id]
        if warehouse_id and warehouse_id in {wh.id for wh in stock_warehouses}
        else [wh.id for wh in stock_warehouses]
    )
    stock_by_item_warehouse = _stock_by_item_and_warehouse(warehouse_ids=selected_ids)
    stock_by_item = {}
    for (nid, _wid), qty in stock_by_item_warehouse.items():
        stock_by_item[nid] = stock_by_item.get(nid, 0) + qty
    data = export_nomenclature_to_excel(items, stock_by_item)
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

    if not _require_edit():
        return redirect(url_for("nomenclature.list_nomenclature"))

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
