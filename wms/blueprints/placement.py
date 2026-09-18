from collections import defaultdict
from datetime import datetime

from flask import (
    Blueprint,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user
from sqlalchemy import func

from ..extensions import db
from ..models import (
    Box,
    BoxItem,
    Cell,
    CELL_CAPACITY,
    MovementLine,
    Nomenclature,
    PlacementDocument,
    PlacementLine,
    UnplacedStock,
    Warehouse,
)
from ..utils.excel_io import export_placement_to_excel, timestamp_for_filename
from ..utils.document_access import ensure_view_document_access, owned_query
from ..utils.http import content_disposition
from ..utils.numbering import next_number

bp = Blueprint("placement", __name__)


@bp.before_request
def _restrict_document_access():
    ensure_view_document_access(PlacementDocument)


def _cell_suggestion_context(warehouse_id):
    """Общие для всех коробов склада данные, нужные _suggest_cell_from_context
    — вынесено отдельно, чтобы считать их ОДИН РАЗ на страницу (см.
    suggest_cells_for_boxes), а не заново на каждый короб: список открытых
    коробов на складе может быть большим (массовое создание коробов), и
    пересчет этих запросов на каждый из них — основная причина, почему
    страница размещения могла долго грузиться.

    Все нижеследующие запросы фильтруют коробы с Box.cell_id.isnot(None) —
    то есть уже РАССТАВЛЕННЫЕ короба. Короб, для которого мы подбираем
    ячейку, сам еще не расставлен (cell_id is None у обоих вызывающих —
    см. suggest_cell/suggest_cells_for_boxes), поэтому исключать его из
    этих запросов явно не нужно, он и так не мог бы туда попасть. Единственное
    исключение — счетчики по складу в целом (warehouse_nom_counts/
    unplaced_nom_counts), которые считаются без фильтра по cell_id и потому
    ЗАХВАТЫВАЮТ сам этот короб — их корректировка на конкретный короб
    происходит уже в _suggest_cell_from_context, в памяти, без лишнего запроса."""
    cells = Cell.query.filter_by(warehouse_id=warehouse_id, is_active=True).all()

    nom_to_cell_ids = {}
    for nid, cell_id_val in (
        db.session.query(BoxItem.nomenclature_id, Box.cell_id)
        .join(Box, BoxItem.box_id == Box.id)
        .filter(Box.warehouse_id == warehouse_id, Box.cell_id.isnot(None))
        .distinct()
        .all()
    ):
        nom_to_cell_ids.setdefault(nid, set()).add(cell_id_val)

    box_counts = dict(
        db.session.query(Box.cell_id, func.count(Box.id))
        .filter(Box.warehouse_id == warehouse_id, Box.cell_id.isnot(None))
        .group_by(Box.cell_id)
        .all()
    )

    # Остатки по товару на складе: всего коробов (в любом статусе) и
    # сколько из них еще не размещено — чтобы понять, "ходовой" ли товар
    # (bulk) и не закончился ли он. Группировка по (nomenclature_id, box_id) —
    # именно короб, а не ячейка, иначе несколько неразмещенных коробов с
    # одним товаром (все с cell_id=None) схлопнулись бы в одну строку.
    warehouse_nom_counts = {}
    unplaced_nom_counts = {}
    for nid, box_id, is_unplaced in (
        db.session.query(BoxItem.nomenclature_id, Box.id, Box.cell_id.is_(None))
        .join(Box, BoxItem.box_id == Box.id)
        .filter(Box.warehouse_id == warehouse_id)
        .distinct()
        .all()
    ):
        warehouse_nom_counts[nid] = warehouse_nom_counts.get(nid, 0) + 1
        if is_unplaced:
            unplaced_nom_counts[nid] = unplaced_nom_counts.get(nid, 0) + 1

    cell_occupant_noms = {}
    for cell_id_val, nid in (
        db.session.query(Box.cell_id, BoxItem.nomenclature_id)
        .join(BoxItem, BoxItem.box_id == Box.id)
        .filter(Box.warehouse_id == warehouse_id, Box.cell_id.isnot(None))
        .distinct()
        .all()
    ):
        cell_occupant_noms.setdefault(cell_id_val, set()).add(nid)

    return {
        "cells": cells,
        "nom_to_cell_ids": nom_to_cell_ids,
        "box_counts": box_counts,
        "warehouse_nom_counts": warehouse_nom_counts,
        "unplaced_nom_counts": unplaced_nom_counts,
        "cell_occupant_noms": cell_occupant_noms,
    }


def _suggest_cell_from_context(ctx, box, box_items=None):
    """Подсказка ячейки под конкретный короб: сначала ищем ячейку, где уже
    лежит короб с тем же товаром (пусть даже вперемешку с другим — это не
    критично), затем — просто ячейку в том же ряду, где такой товар уже
    есть где-нибудь.

    Дальше поведение зависит от того, "ходовой" ли это товар — коробов с
    ним на складе больше, чем вмещает одна ячейка (см. is_bulk ниже). Для
    такого товара выгоднее завести под него отдельную (пустую) ячейку и
    заполнять именно ее, а не распылять его по чужим ячейкам вперемешку —
    поэтому пустая ячейка предпочтительнее уже занятой чем-то другим. По
    той же причине ячейку, целиком занятую под ДРУГОЙ ходовой товар, пока
    у него еще остались неразмещенные короба, стараемся не трогать — как
    только он закончится (неразмещенных коробов не останется), ячейка
    перестает быть "зарезервированной" и снова участвует в общем подборе.
    Для обычного (не ходового) товара поведение прежнее — предпочитаем уже
    начатую ячейку, чтобы не плодить начатые ячейки по одной коробке.

    box_items — заранее загруженный список BoxItem этого короба (см.
    suggest_cells_for_boxes: batch-запрос сразу по всем коробам, чтобы не
    дергать box.items — lazy="dynamic" связь, которая иначе шлет отдельный
    SELECT на каждый короб). None — обычный доступ через box.items, годится
    для одиночного вызова (suggest_cell)."""
    if box_items is None:
        box_items = box.items
    nomenclature_ids = {item.nomenclature_id for item in box_items}
    if not nomenclature_ids:
        return None

    cells = ctx["cells"]
    if not cells:
        return None

    matched_cell_ids = set()
    for nid in nomenclature_ids:
        matched_cell_ids |= ctx["nom_to_cell_ids"].get(nid, set())
    matched_zone_ids = {c.zone_id for c in cells if c.id in matched_cell_ids and c.zone_id}
    box_counts = ctx["box_counts"]

    # ctx["warehouse_nom_counts"]/["unplaced_nom_counts"] считаются по
    # складу в целом, без исключения самого box — вычитаем его вклад здесь,
    # в памяти (эквивалентно фильтру Box.id != box.id в исходной версии,
    # но без отдельного запроса на каждый короб).
    warehouse_nom_counts = dict(ctx["warehouse_nom_counts"])
    unplaced_nom_counts = dict(ctx["unplaced_nom_counts"])
    for nid in nomenclature_ids:
        if nid in warehouse_nom_counts:
            warehouse_nom_counts[nid] -= 1
        if box.cell_id is None and nid in unplaced_nom_counts:
            unplaced_nom_counts[nid] -= 1

    def is_bulk(nid):
        return warehouse_nom_counts.get(nid, 0) > CELL_CAPACITY

    bulk_own = any(is_bulk(nid) for nid in nomenclature_ids)

    cell_occupant_noms = ctx["cell_occupant_noms"]

    best = None
    for cell in cells:
        if cell.id == box.cell_id:
            continue
        count = box_counts.get(cell.id, 0)
        if count >= CELL_CAPACITY:
            continue
        direct_match = cell.id in matched_cell_ids
        row_match = bool(cell.zone_id and cell.zone_id in matched_zone_ids)

        occupants = cell_occupant_noms.get(cell.id, set())
        reserved_for_other_bulk = False
        if not direct_match and len(occupants) == 1:
            (occupant_nid,) = occupants
            reserved_for_other_bulk = (
                occupant_nid not in nomenclature_ids
                and is_bulk(occupant_nid)
                and unplaced_nom_counts.get(occupant_nid, 0) > 0
            )

        mix_penalty = bulk_own and not direct_match and count > 0
        score = (
            not direct_match,
            reserved_for_other_bulk,
            not row_match,
            mix_penalty,
            count if bulk_own else -count,
            cell.code,
        )
        if best is None or score < best[0]:
            best = (score, cell, direct_match, row_match, count)

    if best is None:
        return None
    _, cell, direct_match, row_match, count = best
    if direct_match:
        reason = f"в ячейке уже есть такой же товар ({count} короб. в ячейке)"
    elif bulk_own and count == 0:
        # Приоритетнее row_match: пустая ячейка могла быть выбрана среди
        # нескольких row_match именно из-за bulk-логики (см. mix_penalty
        # выше) — это и есть настоящая причина выбора, а не совпадение ряда.
        reason = "этого товара много на складе — заводим под него отдельную ячейку"
    elif row_match:
        reason = f"такой товар уже есть в этом ряду ({cell.zone.code})"
    elif count > 0:
        reason = "ячейка уже частично заполнена"
    else:
        reason = "пустая ячейка"
    return {"cell": cell, "reason": reason, "free": CELL_CAPACITY - count}


def suggest_cell(warehouse_id, box):
    """Подсказка ячейки для ОДНОГО короба — см. _suggest_cell_from_context
    для самой логики подбора. Строит контекст под этот единственный вызов;
    если нужно подсказать ячейки сразу для многих коробов (страница со
    списком) — использовать suggest_cells_for_boxes, которая считает
    общий для склада контекст один раз, а не на каждый короб заново."""
    ctx = _cell_suggestion_context(warehouse_id)
    return _suggest_cell_from_context(ctx, box)


def suggest_cells_for_boxes(warehouse_id, boxes):
    """Подсказка ячеек сразу для нескольких коробов одного склада — общий
    контекст (список ячеек, занятость, остатки по товару) считается ОДИН
    РАЗ, а не заново на каждый короб, как было бы при вызове suggest_cell
    в цикле. Список открытых коробов на складе (не размещенных) может быть
    большим — массовое создание коробов заготавливает их впрок — и именно
    повторный пересчет одних и тех же запросов на каждый такой короб был
    основной причиной медленной загрузки страницы размещения.

    Состав коробов (BoxItem) тоже загружается одним batch-запросом сразу
    по всем переданным коробам — box.items сам по себе lazy="dynamic" и
    иначе слал бы отдельный SELECT на каждый короб."""
    unplaced_boxes = [box for box in boxes if box.cell_id is None]
    ctx = _cell_suggestion_context(warehouse_id)

    items_by_box_id = {}
    if unplaced_boxes:
        for item in BoxItem.query.filter(
            BoxItem.box_id.in_([box.id for box in unplaced_boxes])
        ).all():
            items_by_box_id.setdefault(item.box_id, []).append(item)

    return {
        box.id: _suggest_cell_from_context(ctx, box, box_items=items_by_box_id.get(box.id, []))
        for box in unplaced_boxes
    }


@bp.route("/")
def list_documents():
    documents = owned_query(PlacementDocument).order_by(PlacementDocument.created_at.desc()).all()

    stock_rows = (
        db.session.query(UnplacedStock)
        .filter(UnplacedStock.qty > 0)
        .join(Warehouse)
        .order_by(Warehouse.code, UnplacedStock.nomenclature_id)
        .all()
    )
    open_boxes = (
        Box.query.filter_by(cell_id=None)
        .join(Warehouse, Box.warehouse_id == Warehouse.id)
        .order_by(Warehouse.code, Box.box_number)
        .all()
    )
    # open_boxes может охватывать сразу несколько складов — контекст подбора
    # ячейки (suggest_cells_for_boxes) считается один раз НА СКЛАД, а не на
    # каждый короб, поэтому группируем по складу перед вызовом.
    boxes_by_warehouse = defaultdict(list)
    for box in open_boxes:
        boxes_by_warehouse[box.warehouse_id].append(box)
    cell_suggestions = {}
    for warehouse_id, boxes in boxes_by_warehouse.items():
        cell_suggestions.update(suggest_cells_for_boxes(warehouse_id, boxes))
    return render_template(
        "placement/list.html",
        documents=documents,
        stock_rows=stock_rows,
        open_boxes=open_boxes,
        cell_suggestions=cell_suggestions,
    )


@bp.route("/write-off-stock", methods=["POST"])
def write_off_stock():
    """Старый адрес оставлен безопасным: возврат создается только в приемке."""
    flash("Возврат поставщику доступен только на этапе разбраковки приемки", "danger")
    return redirect(url_for("placement.list_documents"))


@bp.route("/scan-box")
def scan_box():
    """Быстрое размещение уже упакованного короба (например, приехавшего
    перемещением) по принципу "куда везти короб" в перемещениях —
    сканируем короб, сразу видим рекомендованную ячейку и подтверждаем,
    без захода в какой-либо документ размещения (см. place_box_standalone,
    который уже умеет расставлять короб без документа — этой странице не
    хватало только самого сканирования как отдельного входа, а не строчки
    в общем списке "Короба без ячейки" на placement.list_documents)."""
    box_number = request.args.get("box_number", "").strip()
    box = None
    suggestion = None
    not_found = False
    already_placed = False
    empty_box = False
    if box_number:
        box = Box.find_by_scanned_code(box_number)
        if not box:
            not_found = True
        elif box.cell_id is not None:
            already_placed = True
        elif box.items.count() == 0:
            empty_box = True
        else:
            suggestion = suggest_cell(box.warehouse_id, box)

    return render_template(
        "placement/scan_box.html",
        box_number=box_number,
        box=box,
        suggestion=suggestion,
        not_found=not_found,
        already_placed=already_placed,
        empty_box=empty_box,
    )


@bp.route("/scan-cell")
def scan_cell():
    """Обратный порядок относительно scan_box (там сначала короб, потом
    ячейка) — здесь сначала выбирают склад и сканируют/вводят ЯЧЕЙКУ, а
    потом сканируют в нее короба один за другим без повторного ввода ячейки
    каждый раз. Удобно, когда несколько коробов подряд едут в одно и то же
    место. Код ячейки уникален только в пределах склада (см. Cell.
    __table_args__), поэтому склад выбирается явно, не по одному скану."""
    warehouse_id = request.args.get("warehouse_id", type=int)
    cell_code = request.args.get("cell_code", "").strip()

    warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.code).all()
    warehouse = None
    cell = None
    cell_not_found = False

    if warehouse_id:
        warehouse = Warehouse.query.get(warehouse_id)
        if warehouse and cell_code:
            cell = Cell.query.filter_by(warehouse_id=warehouse_id, code=cell_code).first()
            if not cell:
                cell_not_found = True

    return render_template(
        "placement/scan_cell.html",
        warehouses=warehouses,
        warehouse=warehouse,
        warehouse_id=warehouse_id,
        cell_code=cell_code,
        cell=cell,
        cell_not_found=cell_not_found,
        CELL_CAPACITY=CELL_CAPACITY,
    )


@bp.route("/scan-cell/add-box", methods=["POST"])
def scan_cell_add_box():
    """Разместить очередной короб в уже выбранной (и зафиксированной на
    экране) ячейке — см. scan_cell. Возвращаемся туда же с теми же
    warehouse_id/cell_code, чтобы список коробов в ячейке обновился и можно
    было сразу сканировать следующий короб."""
    warehouse_id = request.form.get("warehouse_id", type=int)
    cell_code = request.form.get("cell_code", "").strip()
    box_number = request.form.get("box_number", "").strip()
    back_url = url_for("placement.scan_cell", warehouse_id=warehouse_id, cell_code=cell_code)

    warehouse = Warehouse.query.get(warehouse_id) if warehouse_id else None
    if not warehouse or not cell_code:
        flash("Сначала выберите склад и ячейку", "danger")
        return redirect(url_for("placement.scan_cell"))

    box = Box.find_by_scanned_code(box_number)
    if not box:
        flash(f"Короб «{box_number}» не найден", "danger")
        return redirect(back_url)
    if box.warehouse_id != warehouse_id:
        flash(f"Короб {box.box_number} числится на складе «{box.warehouse.name}», а не «{warehouse.name}»", "danger")
        return redirect(back_url)
    if box.items.count() == 0:
        flash(f"Короб {box.box_number} пуст — размещать пока нечего", "danger")
        return redirect(back_url)

    error = _place_box(box, cell_code, warehouse_id)
    if error:
        flash(error, "danger")
    else:
        flash(f"Короб {box.box_number} размещен в ячейке {cell_code}", "success")
    return redirect(back_url)


@bp.route("/new", methods=["GET", "POST"])
def new_document():
    if request.method == "GET":
        warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.code).all()
        return render_template("placement/new.html", warehouses=warehouses)

    warehouse_id = request.form.get("warehouse_id", type=int)
    if not warehouse_id:
        flash("Выберите склад размещения", "danger")
        return redirect(url_for("placement.new_document"))

    doc = PlacementDocument(
        number=next_number("placement"),
        warehouse_id=warehouse_id,
        created_by_id=current_user.id,
    )
    db.session.add(doc)
    db.session.commit()
    flash(f"Документ размещения {doc.number} создан", "success")
    return redirect(url_for("placement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>")
def detail(doc_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    unpacked_lines = doc.lines.filter_by(box_id=None).all()
    boxes = doc.boxes.order_by(Box.created_at.asc()).all()
    open_boxes = Box.query.filter_by(warehouse_id=doc.warehouse_id, cell_id=None).all()
    # Пустые короба (заготовлены массовой печатью, но еще ничем не
    # заполнены) не показываем как "неразмещенные" — размещать в ячейку
    # там пока нечего, только замусоривают список. Кол-во товара в коробе
    # считаем одним batch-запросом сразу по всем коробам — box.items.count()
    # в цикле слал бы отдельный SELECT на каждый короб.
    non_empty_box_ids = {
        row[0]
        for row in db.session.query(BoxItem.box_id)
        .filter(BoxItem.box_id.in_([box.id for box in open_boxes]))
        .distinct()
        .all()
    }
    other_open_boxes = [
        box
        for box in open_boxes
        if box.placement_document_id != doc.id and box.id in non_empty_box_ids
    ]
    available_stock = (
        UnplacedStock.query.filter_by(warehouse_id=doc.warehouse_id)
        .filter(UnplacedStock.qty > 0)
        .all()
    )

    # "Активный" короб — выбирается сканированием/созданием и сохраняется в
    # адресе страницы (?box=ID), чтобы дальше сканировать в него товар
    # штрихкод за штрихкодом, без выбора короба из выпадающего списка на
    # каждую позицию (короб — контекст сессии сборки, а не поле формы).
    active_box = None
    active_box_id = request.args.get("box", type=int)
    if active_box_id:
        active_box = Box.query.filter_by(id=active_box_id, warehouse_id=doc.warehouse_id).first()

    cell_suggestions = suggest_cells_for_boxes(doc.warehouse_id, boxes + open_boxes)

    return render_template(
        "placement/detail.html",
        doc=doc,
        unpacked_lines=unpacked_lines,
        boxes=boxes,
        open_boxes=open_boxes,
        other_open_boxes=other_open_boxes,
        available_stock=available_stock,
        active_box=active_box,
        cell_suggestions=cell_suggestions,
    )


@bp.route("/<int:doc_id>/boxes/select", methods=["POST"])
def select_box(doc_id):
    """Сканирование/ввод номера короба — первый шаг размещения. Годится
    любой открытый короб этого склада (в т.ч. заготовленный заранее массовым
    созданием), не только принадлежащий этому документу."""
    doc = PlacementDocument.query.get_or_404(doc_id)
    box_number = request.form.get("box_number", "").strip()
    box = Box.find_by_scanned_code(box_number, warehouse_id=doc.warehouse_id)
    if not box:
        flash(f"Короб '{box_number}' не найден на складе «{doc.warehouse.name}»", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    if box.placement_document_id is None:
        box.placement_document_id = doc.id
    box.mark_scanned(current_user)
    db.session.commit()

    return redirect(url_for("placement.detail", doc_id=doc.id, box=box.id))


def _scan_item_into_box(doc, box, item, qty):
    available = UnplacedStock.available(doc.warehouse_id, item.id)
    if qty <= 0 or qty > available:
        return None, (
            f"Недостаточно неразмещенного остатка «{item.name}»: "
            f"доступно {available} {item.unit}"
        )

    UnplacedStock.consume(doc.warehouse_id, item.id, qty)

    box_item = BoxItem.query.filter_by(box_id=box.id, nomenclature_id=item.id).first()
    if box_item:
        box_item.qty += qty
    else:
        box_item = BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty)
        db.session.add(box_item)

    # Строка размещения сразу с проставленным коробом — для отчета/экспорта
    # и истории, отдельный шаг "упаковать в короб" в этом потоке не нужен.
    line = PlacementLine(document_id=doc.id, nomenclature_id=item.id, qty=qty, box_id=box.id)
    db.session.add(line)
    db.session.commit()
    return line, None


@bp.route("/<int:doc_id>/boxes/<int:box_id>/items/add-by-barcode", methods=["POST"])
def add_item_to_box_by_barcode(doc_id, box_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        return jsonify({"ok": False, "error": "Документ уже завершен"}), 400

    box = Box.query.filter_by(id=box_id, warehouse_id=doc.warehouse_id).first()
    if not box:
        return jsonify({"ok": False, "error": "Короб не найден"}), 404

    barcode = (request.json or {}).get("barcode", "").strip()
    qty = float((request.json or {}).get("qty", 1) or 1)
    item = Nomenclature.query.filter_by(barcode=barcode).first()
    if not item:
        return jsonify({"ok": False, "error": f"Товар со штрихкодом '{barcode}' не найден"}), 404

    line, error = _scan_item_into_box(doc, box, item, qty)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    return jsonify(
        {"ok": True, "line": {"id": line.id, "name": item.name, "qty": line.qty}}
    )


@bp.route("/<int:doc_id>/boxes/<int:box_id>/items/add", methods=["POST"])
def add_item_to_box(doc_id, box_id):
    """Ручное добавление товара в активный короб (поиск "содержит") — то же
    самое действие, что и сканирование штрихкода, только выбор товара через
    автоподбор по названию/артикулу."""
    doc = PlacementDocument.query.get_or_404(doc_id)
    box = Box.query.filter_by(id=box_id, warehouse_id=doc.warehouse_id).first()
    if doc.status != "draft" or not box:
        flash("Документ уже завершен или короб не найден", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id, box=box_id))

    nomenclature_id = request.form.get("nomenclature_id", type=int)
    qty = request.form.get("qty", type=float) or 0
    item = Nomenclature.query.get(nomenclature_id)
    if not item:
        flash("Товар не найден", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id, box=box_id))

    _, error = _scan_item_into_box(doc, box, item, qty)
    if error:
        flash(error, "danger")
    else:
        flash(f"В короб {box.box_number} добавлено: {item.name} ({qty} {item.unit})", "success")
    return redirect(url_for("placement.detail", doc_id=doc.id, box=box_id))


def _add_line(doc, nomenclature, qty):
    available = UnplacedStock.available(doc.warehouse_id, nomenclature.id)
    if qty <= 0 or qty > available:
        return None, (
            f"Недостаточно неразмещенного остатка «{nomenclature.name}»: "
            f"доступно {available} {nomenclature.unit}"
        )

    UnplacedStock.consume(doc.warehouse_id, nomenclature.id, qty)

    line = PlacementLine(document_id=doc.id, nomenclature_id=nomenclature.id, qty=qty)
    db.session.add(line)
    db.session.commit()
    return line, None


@bp.route("/<int:doc_id>/lines/add-by-barcode", methods=["POST"])
def add_line_by_barcode(doc_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        return jsonify({"ok": False, "error": "Документ уже завершен"}), 400

    barcode = (request.json or {}).get("barcode", "").strip()
    qty = float((request.json or {}).get("qty", 1) or 1)
    item = Nomenclature.query.filter_by(barcode=barcode).first()
    if not item:
        return jsonify({"ok": False, "error": f"Товар со штрихкодом '{barcode}' не найден"}), 404

    line, error = _add_line(doc, item, qty)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    return jsonify(
        {"ok": True, "line": {"id": line.id, "name": item.name, "sku": item.sku, "qty": line.qty}}
    )


@bp.route("/<int:doc_id>/lines/add", methods=["POST"])
def add_line(doc_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    nomenclature_id = request.form.get("nomenclature_id", type=int)
    qty = request.form.get("qty", type=float) or 0

    item = Nomenclature.query.get(nomenclature_id)
    if not item:
        flash("Товар не найден", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    line, error = _add_line(doc, item, qty)
    if error:
        flash(error, "danger")
    else:
        flash(f"Добавлено под упаковку: {item.name} ({qty} {item.unit})", "success")
    return redirect(url_for("placement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/lines/<int:line_id>/delete", methods=["POST"])
def delete_line(doc_id, line_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    line = PlacementLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()
    if line.box_id is not None:
        flash("Нельзя удалить строку, уже упакованную в короб", "danger")
        return redirect(url_for("placement.detail", doc_id=doc_id))

    UnplacedStock.add(doc.warehouse_id, line.nomenclature_id, line.qty)
    db.session.delete(line)
    db.session.commit()
    return redirect(url_for("placement.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/boxes/create", methods=["POST"])
def create_box(doc_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    box = Box(
        box_number=next_number("box"),
        warehouse_id=doc.warehouse_id,
        placement_document_id=doc.id,
        status="open",
    )
    db.session.add(box)
    db.session.commit()
    flash(f"Короб {box.box_number} создан", "success")
    return redirect(url_for("placement.detail", doc_id=doc.id, box=box.id))


@bp.route("/<int:doc_id>/lines/<int:line_id>/pack", methods=["POST"])
def pack_line(doc_id, line_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    line = PlacementLine.query.filter_by(id=line_id, document_id=doc.id).first_or_404()
    box_id = request.form.get("box_id", type=int)
    qty = request.form.get("qty", type=float)

    # Короб может быть создан прямо в этом документе или заготовлен заранее
    # (массовое создание в «Склады → Массовое создание коробов») — годится
    # любой короб этого склада, в том числе уже размещенный в ячейке (можно
    # доукомплектовать короб товаром и после того, как его расставили).
    box = Box.query.filter_by(id=box_id, warehouse_id=doc.warehouse_id).first()
    if not box:
        flash("Короб не найден", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    if line.box_id is not None:
        flash("Строка уже упакована", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    if qty is None or qty <= 0 or qty > line.qty:
        qty = line.qty

    if box.placement_document_id is None:
        box.placement_document_id = doc.id

    box_item = BoxItem(box_id=box.id, nomenclature_id=line.nomenclature_id, qty=qty)
    db.session.add(box_item)

    if qty >= line.qty:
        line.box_id = box.id
    else:
        line.qty -= qty
        packed_line = PlacementLine(
            document_id=doc.id,
            nomenclature_id=line.nomenclature_id,
            qty=qty,
            box_id=box.id,
        )
        db.session.add(packed_line)

    db.session.commit()
    flash(f"Товар упакован в короб {box.box_number}", "success")
    return redirect(url_for("placement.detail", doc_id=doc.id))


def _place_box(box, cell_code, expected_warehouse_id):
    if not cell_code:
        return "Укажите или отсканируйте код ячейки"

    cell = Cell.query.filter_by(warehouse_id=expected_warehouse_id, code=cell_code).first()
    if not cell:
        return f"Ячейка '{cell_code}' не найдена на этом складе"

    if cell.id != box.cell_id and cell.free_space() <= 0:
        return f"Ячейка '{cell_code}' заполнена (вмещает {CELL_CAPACITY} коробов)"

    box.cell_id = cell.id
    box.status = "stored"
    box.mark_scanned(current_user)
    db.session.commit()
    return None


@bp.route("/<int:doc_id>/boxes/<int:box_id>/place", methods=["POST"])
def place_box(doc_id, box_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    # Разместить можно любой открытый короб этого склада — не только упакованный
    # в рамках именно этого документа (например, короб мог приехать перемещением
    # без ячейки и теперь ждет размещения).
    box = Box.query.filter_by(id=box_id, warehouse_id=doc.warehouse_id).first_or_404()

    error = _place_box(box, request.form.get("cell_code", "").strip(), doc.warehouse_id)
    if error:
        flash(error, "danger")
    else:
        flash(f"Короб {box.box_number} размещен в ячейке {box.cell.code}", "success")
    return redirect(url_for("placement.detail", doc_id=doc.id))


@bp.route("/box/<int:box_id>/place", methods=["POST"])
def place_box_standalone(box_id):
    """Быстрое размещение уже упакованного короба без ячейки (например,
    приехавшего перемещением) — без создания отдельного документа."""
    box = Box.query.get_or_404(box_id)
    next_url = request.form.get("next") or url_for("placement.list_documents")

    error = _place_box(box, request.form.get("cell_code", "").strip(), box.warehouse_id)
    if error:
        flash(error, "danger")
    else:
        flash(f"Короб {box.box_number} размещен в ячейке {box.cell.code}", "success")
    return redirect(next_url)


@bp.route("/<int:doc_id>/delete", methods=["POST"])
def delete_document(doc_id):
    """Удаляет черновик размещения целиком: неупакованный остаток и остаток,
    упакованный в короба этого документа, возвращается обратно в
    неразмещенный остаток склада, сами короба (и их содержимое) удаляются.
    Если какой-то короб уже успел уехать перемещением — удалить нельзя,
    чтобы не потерять историю и не рассинхронизировать расположение."""
    if not current_user.is_admin:
        flash("Удалять документы может только администратор", "danger")
        return redirect(url_for("placement.detail", doc_id=doc_id))

    doc = PlacementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Можно удалить только черновик — завершенный документ уже разместил товар в ячейках", "danger")
        return redirect(url_for("placement.detail", doc_id=doc_id))

    boxes = doc.boxes.all()
    for box in boxes:
        if MovementLine.query.filter_by(box_id=box.id).first():
            flash(
                f"Нельзя удалить: короб {box.box_number} уже участвует в перемещении",
                "danger",
            )
            return redirect(url_for("placement.detail", doc_id=doc_id))

    for line in doc.lines.filter_by(box_id=None).all():
        UnplacedStock.add(doc.warehouse_id, line.nomenclature_id, line.qty)

    for box in boxes:
        for item in box.items:
            UnplacedStock.add(doc.warehouse_id, item.nomenclature_id, item.qty)
        db.session.delete(box)

    number = doc.number
    db.session.delete(doc)
    db.session.commit()
    flash(f"Документ размещения {number} удален, остаток возвращен в неразмещенный", "success")
    return redirect(url_for("placement.list_documents"))


@bp.route("/<int:doc_id>/complete", methods=["POST"])
def complete(doc_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    if doc.lines.filter_by(box_id=None).count() > 0:
        flash("Не все позиции упакованы в короба", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    if doc.boxes.count() == 0:
        flash("Нет коробов для завершения размещения", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    unplaced = [b for b in doc.boxes if b.cell_id is None]
    if unplaced:
        names = ", ".join(b.box_number for b in unplaced)
        flash(f"Не все короба размещены в ячейках: {names}", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    doc.status = "completed"
    doc.completed_at = datetime.utcnow()
    db.session.commit()
    flash(f"Размещение {doc.number} завершено", "success")
    return redirect(url_for("placement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/export.xlsx")
def export_document(doc_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    data = export_placement_to_excel([doc])
    fname = f"{doc.number}_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )
