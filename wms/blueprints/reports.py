import statistics
from collections import defaultdict
from datetime import datetime

from flask import Blueprint, Response, render_template, request

from ..extensions import db
from ..models import (
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    MovementReceiptDiscrepancy,
    Nomenclature,
    OneCQuantityCheck,
    PlacementDocument,
    ProductCategory,
    ReceivingDocument,
    ReceivingLine,
    Warehouse,
)
from ..utils.excel_io import (
    export_movement_to_excel,
    export_placement_to_excel,
    export_receiving_status_report_to_excel,
    export_receiving_to_excel,
    export_shipped_report_to_excel,
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
        query = query.filter(
            (MovementDocument.from_warehouse_id == warehouse_id)
            | (MovementDocument.to_warehouse_id == warehouse_id)
        )
    if date_from:
        query = query.filter(MovementDocument.created_at >= date_from)
    if date_to:
        query = query.filter(MovementDocument.created_at < date_to)
    return query.order_by(MovementDocument.created_at.desc()).all()


RECEIVING_STATUS_LABELS = {
    "draft": "Черновик",
    "recounting": "На пересчете",
    "sorting": "На разбраковке",
    "completed": "Завершена",
}


def _receiving_status_since(doc):
    """С какого момента документ находится в ТЕКУЩЕМ статусе — для
    draft/completed это просто created_at/completed_at, а для
    recounting/sorting берем отметку перехода (см. ReceivingDocument.
    recounting_started_at/sorting_started_at); нет отметки (приемка
    завершена до появления этих полей) — откатываемся на created_at,
    чтобы отчет не падал на старых документах."""
    if doc.status == "recounting":
        return doc.recounting_started_at or doc.created_at
    if doc.status == "sorting":
        return doc.sorting_started_at or doc.created_at
    if doc.status == "completed":
        return doc.completed_at or doc.created_at
    return doc.created_at


def _receiving_status_rows():
    """Для каждой приемки — статус, сколько дней она в нем висит, суммарное
    кол-во товара по строкам и вид(ы) товара (ProductCategory.name, см. ее
    докстринг — это и есть "вид товара" в терминах WMS). Помогает найти
    заявки, застрявшие на каком-то этапе дольше обычного."""
    unfinished_only = request.args.get("unfinished") == "on"
    documents = _filtered_receiving()
    if unfinished_only:
        documents = [d for d in documents if d.status != "completed"]

    doc_ids = [d.id for d in documents]
    qty_by_doc = defaultdict(float)
    category_ids_by_doc = defaultdict(set)
    if doc_ids:
        line_rows = (
            db.session.query(
                ReceivingLine.document_id,
                ReceivingLine.qty,
                Nomenclature.category_id,
            )
            .join(Nomenclature, ReceivingLine.nomenclature_id == Nomenclature.id)
            .filter(ReceivingLine.document_id.in_(doc_ids))
            .all()
        )
        category_ids = {r.category_id for r in line_rows if r.category_id is not None}
        category_names = (
            {c.id: c.name for c in ProductCategory.query.filter(ProductCategory.id.in_(category_ids)).all()}
            if category_ids
            else {}
        )
        for document_id, qty, category_id in line_rows:
            qty_by_doc[document_id] += qty
            if category_id is not None:
                category_ids_by_doc[document_id].add(category_names.get(category_id, ""))

    now = datetime.utcnow()
    rows = []
    for doc in documents:
        since = _receiving_status_since(doc)
        rows.append(
            {
                "document": doc,
                "status_label": RECEIVING_STATUS_LABELS.get(doc.status, doc.status),
                "days_in_status": (now - since).days if since else None,
                "qty": qty_by_doc.get(doc.id, 0),
                "categories": ", ".join(sorted(c for c in category_ids_by_doc.get(doc.id, set()) if c)),
            }
        )
    rows.sort(key=lambda r: r["days_in_status"] or 0, reverse=True)
    return rows


@bp.route("/receiving-status")
def receiving_status_report():
    rows = _receiving_status_rows()
    warehouses = Warehouse.query.order_by(Warehouse.code).all()
    return render_template(
        "reports/receiving_status.html",
        rows=rows,
        warehouses=warehouses,
        selected_warehouse_id=request.args.get("warehouse_id", type=int),
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
        unfinished_only=request.args.get("unfinished") == "on",
    )


@bp.route("/receiving-status.xlsx")
def receiving_status_report_export():
    rows = _receiving_status_rows()
    data = export_receiving_status_report_to_excel(rows)
    fname = f"receiving_status_report_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


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


@bp.route("/movement-shortages")
def movement_shortages_report():
    """Документы перемещения, по которым маркетплейс принял меньше товара.

    Детализация уже хранится в MovementReceiptDiscrepancy после действия
    «Принято на складе», поэтому отчет не копирует данные в отдельный реестр.
    """
    warehouse_id = request.args.get("warehouse_id", type=int)
    date_from = _parse_date(request.args.get("date_from"))
    date_to = _parse_date(request.args.get("date_to"))

    query = (
        MovementDocument.query
        .join(
            MovementReceiptDiscrepancy,
            MovementReceiptDiscrepancy.document_id == MovementDocument.id,
        )
        .filter(
            MovementReceiptDiscrepancy.received_qty
            < MovementReceiptDiscrepancy.expected_qty
        )
    )
    if warehouse_id:
        query = query.filter(MovementDocument.to_warehouse_id == warehouse_id)
    if date_from:
        query = query.filter(MovementDocument.received_at >= date_from)
    if date_to:
        query = query.filter(MovementDocument.received_at < date_to)

    documents = query.order_by(MovementDocument.received_at.desc()).all()
    warehouses = Warehouse.query.order_by(Warehouse.code).all()
    return render_template(
        "reports/movement_shortages.html",
        documents=documents,
        warehouses=warehouses,
        selected_warehouse_id=warehouse_id,
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
    )


@bp.route("/one-c-quantity-mismatches")
def one_c_quantity_mismatches():
    rows = OneCQuantityCheck.query.order_by(
        OneCQuantityCheck.checked_at.desc(), OneCQuantityCheck.document_number
    ).all()
    return render_template("reports/one_c_quantity_mismatches.html", rows=rows)


def _shipped_rows():
    """Сколько и какого товара реально отгружено (перемещение со склада
    отправки завершено — товар физически уехал) по складам назначения, за
    период. Не путать с «Планом отгрузок» (это план/факт по маркетплейсам)
    — здесь просто фактически отгруженные количества, по любым складам."""
    warehouse_id = request.args.get("warehouse_id", type=int)
    date_from = _parse_date(request.args.get("date_from"))
    date_to = _parse_date(request.args.get("date_to"))

    query = (
        db.session.query(
            MovementDocument.to_warehouse_id,
            BoxItem.nomenclature_id,
            db.func.sum(BoxItem.qty).label("qty"),
        )
        .join(MovementLine, MovementLine.document_id == MovementDocument.id)
        .join(BoxItem, BoxItem.box_id == MovementLine.box_id)
        .filter(MovementDocument.status == "completed")
    )
    if warehouse_id:
        query = query.filter(MovementDocument.to_warehouse_id == warehouse_id)
    if date_from:
        query = query.filter(MovementDocument.completed_at >= date_from)
    if date_to:
        query = query.filter(MovementDocument.completed_at < date_to)

    grouped = query.group_by(MovementDocument.to_warehouse_id, BoxItem.nomenclature_id).all()

    warehouses_by_id = {w.id: w for w in Warehouse.query.all()}
    nomenclature_ids = [r.nomenclature_id for r in grouped]
    items_by_id = (
        {n.id: n for n in Nomenclature.query.filter(Nomenclature.id.in_(nomenclature_ids)).all()}
        if nomenclature_ids
        else {}
    )

    rows = [
        {
            "warehouse": warehouses_by_id.get(r.to_warehouse_id),
            "nomenclature": items_by_id.get(r.nomenclature_id),
            "qty": r.qty,
        }
        for r in grouped
    ]
    rows.sort(
        key=lambda row: (
            row["warehouse"].name if row["warehouse"] else "",
            row["nomenclature"].name if row["nomenclature"] else "",
        )
    )
    return rows


@bp.route("/shipped")
def shipped_report():
    rows = _shipped_rows()
    warehouses = Warehouse.query.order_by(Warehouse.code).all()
    return render_template(
        "reports/shipped.html",
        rows=rows,
        warehouses=warehouses,
        selected_warehouse_id=request.args.get("warehouse_id", type=int),
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
    )


@bp.route("/shipped.xlsx")
def shipped_report_export():
    rows = _shipped_rows()
    data = export_shipped_report_to_excel(rows)
    fname = f"shipped_report_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


BOX_ANOMALY_RATIO_DEFAULT = 3.0
# Группе нужно минимум 2 короба (сам + хотя бы один для сравнения), иначе
# медиана — это просто сам товар и сравнивать не с чем.
BOX_ANOMALY_MIN_GROUP_SIZE = 2


def _box_anomaly_group_key(nomenclature):
    """Группа для сравнения "типичного" количества в коробе. Лучший
    случай — категория (вид товара) + размер сразу по всем цветам/SKU
    этой связки (одна и та же модель разного цвета того же размера
    обычно упаковывается одинаково). Если у товара не заполнена
    категория или размер — сравнивать по этому признаку не с чем, но это
    не повод пропускать проверку вообще: откатываемся на сравнение
    короба с другими коробами ТОГО ЖЕ SKU (без кросс-цветового
    сопоставления, но хоть какая-то проверка лучше отсутствия проверки)."""
    if nomenclature.category_id is not None and nomenclature.size is not None:
        return ("cat_size", nomenclature.category_id, nomenclature.size)
    return ("sku", nomenclature.id)


def _box_anomaly_rows(warehouse_id=None, ratio_threshold=BOX_ANOMALY_RATIO_DEFAULT):
    """Ищет короба с подозрительным количеством товара — сравнивает qty
    каждой строки короба с медианой ОСТАЛЬНЫХ записей той же группы (см.
    _box_anomaly_group_key).

    Медиана считается БЕЗ самой сравниваемой записи (leave-one-out), а не
    по группе целиком — иначе при маленькой группе (например, всего 2
    короба — по одному на каждый цвет, самый частый случай) сам выброс
    сдвигает медиану к себе и перестает быть заметен: короб с 10-кратным
    перебором и обычный короб дали бы почти одинаковое отношение к общей
    медиане, и аномалия осталась бы незамеченной.

    Медиана считается по ВСЕМ коробам во всех складах (чем больше
    выборка, тем надежнее "типичное" значение), а склад из фильтра
    сужает только то, что показываем."""
    all_items = (
        db.session.query(BoxItem, Box, Nomenclature)
        .join(Box, BoxItem.box_id == Box.id)
        .join(Nomenclature, BoxItem.nomenclature_id == Nomenclature.id)
        .all()
    )

    groups = defaultdict(list)
    for box_item, _box, nomenclature in all_items:
        groups[_box_anomaly_group_key(nomenclature)].append(box_item.qty)

    rows = []
    for box_item, box, nomenclature in all_items:
        if warehouse_id and box.warehouse_id != warehouse_id:
            continue
        group = groups[_box_anomaly_group_key(nomenclature)]
        if len(group) < BOX_ANOMALY_MIN_GROUP_SIZE:
            continue
        others = list(group)
        others.remove(box_item.qty)
        median = statistics.median(others)
        if not median:
            continue
        ratio = box_item.qty / median
        if ratio >= ratio_threshold or ratio <= 1 / ratio_threshold:
            rows.append(
                {
                    "box": box,
                    "nomenclature": nomenclature,
                    "qty": box_item.qty,
                    "median": median,
                    "ratio": ratio,
                    "group_size": len(group),
                }
            )

    rows.sort(key=lambda r: max(r["ratio"], 1 / r["ratio"]), reverse=True)
    return rows


def _boxes_by_nomenclature(nomenclature_ids):
    """{nomenclature_id: [{"box_number", "warehouse", "qty"}, ...]} — ВСЕ
    короба (по всем складам, без учета фильтра отчета — как и медиана в
    _box_anomaly_rows) с этим товаром, отсортированные по количеству. Дает
    возможность сверить конкретную аномальную строку с остальными
    коробами того же товара и понять, ошибка это приемки или нормальная
    вариация (см. reports/box_anomalies.html)."""
    if not nomenclature_ids:
        return {}
    result = defaultdict(list)
    query = (
        db.session.query(BoxItem.nomenclature_id, Box.box_number, Warehouse.name, BoxItem.qty)
        .join(Box, BoxItem.box_id == Box.id)
        .outerjoin(Warehouse, Box.warehouse_id == Warehouse.id)
        .filter(BoxItem.nomenclature_id.in_(nomenclature_ids))
    )
    for nomenclature_id, box_number, warehouse_name, qty in query.all():
        result[nomenclature_id].append(
            {"box_number": box_number, "warehouse": warehouse_name or "—", "qty": qty}
        )
    for boxes in result.values():
        boxes.sort(key=lambda b: b["qty"], reverse=True)
    return result


@bp.route("/box-anomalies")
def box_anomalies_report():
    warehouse_id = request.args.get("warehouse_id", type=int)
    threshold = request.args.get("threshold", type=float) or BOX_ANOMALY_RATIO_DEFAULT
    if threshold <= 1:
        threshold = BOX_ANOMALY_RATIO_DEFAULT
    rows = _box_anomaly_rows(warehouse_id=warehouse_id, ratio_threshold=threshold)
    warehouses = Warehouse.query.order_by(Warehouse.code).all()
    boxes_by_nomenclature = _boxes_by_nomenclature({row["nomenclature"].id for row in rows})
    return render_template(
        "reports/box_anomalies.html",
        rows=rows,
        warehouses=warehouses,
        selected_warehouse_id=warehouse_id,
        threshold=threshold,
        boxes_by_nomenclature=boxes_by_nomenclature,
    )
