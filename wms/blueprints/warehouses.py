import re
import unicodedata

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import Box, Cell, ShipmentPlanLine, UnplacedStock, Warehouse, Zone
from ..utils.numbering import next_number

bp = Blueprint("warehouses", __name__)

# Ширина числовой части кода ячейки: код ряда "A" -> ячейки "A0001", "A0002", ...
# Ряд остается моделью Zone в БД (менять таблицу/класс ради переименования
# в интерфейсе избыточно) — но по смыслу и в тексте для пользователя это
# именно "ряд", а не свободно называемая "зона".
CELL_NUMBER_WIDTH = 4

# Известные соответствия "город WMS -> склад 1С" для автоматического
# заполнения Warehouse.fulfillment_1c_name (см. модель) — ключи в нижнем
# регистре, сверяются через default_fulfillment_1c_name(). Несколько
# городов (Черкесск и Пятигорск) намеренно указывают на один и тот же склад
# 1С — обе точки физически обслуживает один и тот же промежуточный склад
# фулфилмента. Администратор может поправить/дополнить список на странице
# «Настройки», это только стартовые значения.
FULFILLMENT_1C_DEFAULTS = {
    "екатеринбург": "Товары в пути ФФ ЕКБ",
    "екб": "Товары в пути ФФ ЕКБ",
    "казань": "Товары в пути ФФ КАЗАНЬ, Взлётная 30",
    "краснодар": "Товары в пути ФФ КРАСНОДАР",
    "москва": "Товары в пути ФФ МОСКВА",
    "новосибирск": "Товары в пути ФФ НОВОСИБИРСК",
    "самара": "Товары в пути ФФ САМАРА",
    "санкт-петербург": "Товары в пути ФФ СПБ",
    "спб": "Товары в пути ФФ СПБ",
    "питер": "Товары в пути ФФ СПБ",
    "черкесск": "Товары в пути ФФ ЧЕРКЕССК (Лейла)",
    "пятигорск": "Товары в пути ФФ ЧЕРКЕССК (Лейла)",
}

_CITY_ALIASES = {
    "екб": "Екатеринбург",
    "екатеринбург": "Екатеринбург",
    "спб": "Санкт-Петербург",
    "питер": "Санкт-Петербург",
    "санкт петербург": "Санкт-Петербург",
    "санкт-петербург": "Санкт-Петербург",
    "москва": "Москва",
}
_MARKETPLACE_PREFIX_RE = re.compile(
    r"^(?:озон|ozon|вб|wb)\s*(?::|[-–—])?\s*", re.IGNORECASE
)


def canonical_marketplace_city(value):
    """Каноническое название направления из заголовка плана.

    Маркетплейс не является частью города и хранится в Warehouse.marketplace.
    Москва, Москва 1 и Москва 2 намеренно остаются тремя разными ключами.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).replace("ё", "е")
    text = _MARKETPLACE_PREFIX_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" \t,;:")
    key = text.casefold()
    key = re.sub(r"\s*[-–—]\s*", "-", key)
    if key in _CITY_ALIASES:
        return _CITY_ALIASES[key]
    moscow = re.fullmatch(r"москва\s*([12])", key)
    if moscow:
        return f"Москва {moscow.group(1)}"
    return "-".join(part.capitalize() for part in key.split("-"))


def _replace_foreign_keys(target_table_name, old_id, new_id):
    """Переносит все ссылки на запись справочника через метаданные моделей."""
    for table in db.metadata.sorted_tables:
        for column in table.columns:
            if any(
                fk.column.table.name == target_table_name and fk.column.name == "id"
                for fk in column.foreign_keys
            ):
                db.session.execute(
                    table.update().where(column == old_id).values({column.name: new_id})
                )


def _merge_marketplace_warehouse(primary, duplicate):
    """Без потери истории сводит склад-дубль в каноническое направление."""
    if not primary.address and duplicate.address:
        primary.address = duplicate.address
    if not primary.recipient_info and duplicate.recipient_info:
        primary.recipient_info = duplicate.recipient_info
    if not primary.fulfillment_1c_name and duplicate.fulfillment_1c_name:
        primary.fulfillment_1c_name = duplicate.fulfillment_1c_name
    primary.is_active = primary.is_active or duplicate.is_active

    # Агрегаты с уникальностью по складу нельзя переносить простым UPDATE.
    for source in UnplacedStock.query.filter_by(warehouse_id=duplicate.id).all():
        target = UnplacedStock.query.filter_by(
            warehouse_id=primary.id, nomenclature_id=source.nomenclature_id
        ).first()
        if target:
            target.qty += source.qty
            db.session.delete(source)
        else:
            source.warehouse_id = primary.id

    for source in ShipmentPlanLine.query.filter_by(warehouse_id=duplicate.id).all():
        target = ShipmentPlanLine.query.filter_by(
            plan_id=source.plan_id, warehouse_id=primary.id, barcode=source.barcode
        ).first()
        if target:
            target.planned_qty += source.planned_qty
            target.fulfilled_qty += source.fulfilled_qty
            dates = [d for d in (target.period_start, source.period_start) if d]
            target.period_start = max(dates) if dates else None
            db.session.delete(source)
        else:
            source.warehouse_id = primary.id

    # Если на дублях успели создать одинаковые ячейки, сохраняем одну ячейку
    # и переносим на нее короба/документы. Разные коды просто переезжают.
    for source in Cell.query.filter_by(warehouse_id=duplicate.id).all():
        target = Cell.query.filter_by(warehouse_id=primary.id, code=source.code).first()
        if target:
            _replace_foreign_keys("cells", source.id, target.id)
            db.session.delete(source)
        else:
            source.warehouse_id = primary.id

    db.session.flush()
    for source in Zone.query.filter_by(warehouse_id=duplicate.id).all():
        target = Zone.query.filter_by(warehouse_id=primary.id, code=source.code).first()
        if target:
            Cell.query.filter_by(zone_id=source.id).update(
                {Cell.zone_id: target.id}, synchronize_session=False
            )
            db.session.delete(source)
        else:
            source.warehouse_id = primary.id

    db.session.flush()
    _replace_foreign_keys("warehouses", duplicate.id, primary.id)
    db.session.delete(duplicate)
    db.session.flush()


def consolidate_marketplace_warehouses(marketplace=None):
    """Нормализует направления и объединяет старые дубли одного маркетплейса."""
    query = Warehouse.query.filter(Warehouse.marketplace.isnot(None))
    if marketplace:
        query = query.filter_by(marketplace=marketplace)
    groups = {}
    for warehouse in query.order_by(Warehouse.id).all():
        city = canonical_marketplace_city(warehouse.marketplace_city or warehouse.name)
        if city:
            groups.setdefault((warehouse.marketplace, city.casefold()), []).append(warehouse)

    for (warehouse_marketplace, _city_key), warehouses in groups.items():
        city = canonical_marketplace_city(
            warehouses[0].marketplace_city or warehouses[0].name
        )
        primary = next(
            (
                wh
                for wh in warehouses
                if canonical_marketplace_city(wh.marketplace_city) == city
                and wh.marketplace_city == city
            ),
            warehouses[0],
        )
        for duplicate in warehouses:
            if duplicate.id != primary.id:
                _merge_marketplace_warehouse(primary, duplicate)
        primary.marketplace = warehouse_marketplace
        primary.marketplace_city = city
        primary.name = city
    db.session.flush()


def default_fulfillment_1c_name(city):
    """Стартовая догадка склада 1С по названию города WMS (см.
    FULFILLMENT_1C_DEFAULTS) — None, если город не из списка известных
    (тогда поле остается пустым для ручного заполнения администратором)."""
    if not city:
        return None
    return FULFILLMENT_1C_DEFAULTS.get(canonical_marketplace_city(city).casefold())


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
    fulfillment_warehouses = [wh for wh in warehouses if wh.marketplace is not None]
    return render_template(
        "warehouses/list.html",
        warehouses=warehouses,
        fulfillment_warehouses=fulfillment_warehouses,
    )


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


@bp.route("/<int:warehouse_id>/recipient", methods=["POST"])
def update_recipient(warehouse_id):
    """Получатель для этого склада-направления — печатается на стикерах
    отправления (см. movement.export_shipping_labels). Настраивается
    администратором на странице «Настройки» отдельно для каждого склада,
    в первую очередь для складов-городов маркетплейсов, куда физически
    едут короба."""
    if not current_user.is_admin:
        flash("Настраивать получателей может только администратор", "danger")
        return redirect(url_for("warehouses.list_warehouses"))

    wh = Warehouse.query.get_or_404(warehouse_id)
    wh.recipient_info = request.form.get("recipient_info", "").strip() or None
    db.session.commit()
    flash(f"Получатель для «{wh.name}» обновлен", "success")
    return redirect(url_for("auth.users"))


@bp.route("/<int:warehouse_id>/fulfillment-1c-name", methods=["POST"])
def update_fulfillment_1c_name(warehouse_id):
    """Склад 1С для КОНКРЕТНОГО склада-города — раздельно для ОЗОН и ВБ даже
    в одном городе: одна и та же площадка одного города может возить на
    разные склады 1С в зависимости от маркетплейса (например, ВБ Краснодар
    едет на СЦ, а ОЗОН Краснодар — на фулфилмент), так что общее значение
    сразу на оба маркетплейса города не подходит — настраивается отдельно
    на каждый склад, как и получатель на стикерах (см. update_recipient).
    См. Warehouse.fulfillment_1c_name и integration_1c._to_warehouse_name_for_1c."""
    if not current_user.can_manage_warehouse_mapping():
        flash("У вас нет права на сопоставление складов с 1С", "danger")
        return redirect(url_for("warehouses.list_warehouses"))

    wh = Warehouse.query.get_or_404(warehouse_id)
    wh.fulfillment_1c_name = request.form.get("fulfillment_1c_name", "").strip() or None
    db.session.commit()
    if wh.fulfillment_1c_name:
        flash(f"Склад 1С для «{wh.name}» обновлен: «{wh.fulfillment_1c_name}»", "success")
    else:
        flash(f"Склад 1С для «{wh.name}» очищен — будет использован общий запасной склад", "success")
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
