from flask import Blueprint, Response, abort, render_template, request

from ..models import Box, Cell, Nomenclature, Zone
from ..utils.barcodes import generate_barcode_data_uri
from ..utils.http import content_disposition
from ..utils.labels_pdf import build_label_pdf, build_labels_batch_pdf, build_zone_label_pdf

bp = Blueprint("labels", __name__)


def _autoprint():
    return request.args.get("autoprint", "1") != "0"


# ---------- Этикетка товара ----------


@bp.route("/item/<int:item_id>")
def item_label(item_id):
    item = Nomenclature.query.get_or_404(item_id)
    img = generate_barcode_data_uri(item.barcode)
    return render_template(
        "labels/item.html", item=item, barcode_img=img, autoprint=_autoprint()
    )


@bp.route("/item/<int:item_id>.pdf")
def item_label_pdf(item_id):
    item = Nomenclature.query.get_or_404(item_id)
    pdf = build_label_pdf(item.barcode, item.name, f"арт. {item.sku}")
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition(f"item_{item.sku}.pdf", "inline")},
    )


# ---------- Этикетка короба ----------


@bp.route("/box/<int:box_id>")
def box_label(box_id):
    box = Box.query.get_or_404(box_id)
    img = generate_barcode_data_uri(box.box_number)
    return render_template("labels/box.html", box=box, barcode_img=img, autoprint=_autoprint())


@bp.route("/box/<int:box_id>.pdf")
def box_label_pdf(box_id):
    box = Box.query.get_or_404(box_id)
    subtitle = box.warehouse.name if box.warehouse else ""
    pdf = build_label_pdf(box.box_number, f"Короб {box.box_number}", subtitle)
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition(f"{box.box_number}.pdf", "inline")},
    )


@bp.route("/boxes/batch.pdf")
def boxes_label_batch_pdf():
    """Печать этикеток сразу нескольких коробов одним PDF (например, для
    только что созданной массовой партии) — ?ids=1,2,3."""
    ids_param = request.args.get("ids", "")
    try:
        ids = [int(v) for v in ids_param.split(",") if v.strip()]
    except ValueError:
        abort(400, "Некорректный список коробов")
    if not ids:
        abort(404, "Список коробов пуст")

    boxes = Box.query.filter(Box.id.in_(ids)).all()
    boxes_by_id = {b.id: b for b in boxes}
    entries = []
    for box_id in ids:
        box = boxes_by_id.get(box_id)
        if not box:
            continue
        subtitle = box.warehouse.name if box.warehouse else ""
        entries.append((box.box_number, f"Короб {box.box_number}", subtitle))

    if not entries:
        abort(404, "Короба не найдены")

    pdf = build_labels_batch_pdf(entries)
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition("boxes_batch.pdf", "inline")},
    )


@bp.route("/box/<int:box_id>/items")
def box_items_labels(box_id):
    """Печать этикеток всех товаров, упакованных в короб (для наклейки на каждую позицию)."""
    box = Box.query.get_or_404(box_id)
    labels = []
    for line in box.items:
        labels.append(
            {
                "item": line.nomenclature,
                "qty": line.qty,
                "barcode_img": generate_barcode_data_uri(line.nomenclature.barcode),
            }
        )
    if not labels:
        abort(404, "В коробе нет товаров")
    return render_template(
        "labels/box_items.html", box=box, labels=labels, autoprint=_autoprint()
    )


# ---------- Этикетка ячейки ----------


@bp.route("/cell/<int:cell_id>")
def cell_label(cell_id):
    cell = Cell.query.get_or_404(cell_id)
    img = generate_barcode_data_uri(cell.code)
    return render_template("labels/cell.html", cell=cell, barcode_img=img, autoprint=_autoprint())


@bp.route("/cell/<int:cell_id>.pdf")
def cell_label_pdf(cell_id):
    cell = Cell.query.get_or_404(cell_id)
    subtitle = cell.warehouse.name if cell.warehouse else ""
    pdf = build_label_pdf(cell.code, f"Ячейка {cell.code}", subtitle)
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition(f"cell_{cell.code}.pdf", "inline")},
    )


# ---------- Этикетка зоны (A4) ----------


@bp.route("/zone/<int:zone_id>")
def zone_label(zone_id):
    zone = Zone.query.get_or_404(zone_id)
    img = generate_barcode_data_uri(zone.code)
    cell_codes = [c.code for c in zone.cells.order_by(Cell.code).all()]
    return render_template(
        "labels/zone.html", zone=zone, barcode_img=img, cell_codes=cell_codes, autoprint=_autoprint()
    )


@bp.route("/zone/<int:zone_id>.pdf")
def zone_label_pdf(zone_id):
    zone = Zone.query.get_or_404(zone_id)
    subtitle = zone.warehouse.name if zone.warehouse else ""
    title = f"Зона {zone.code}" + (f" — {zone.name}" if zone.name else "")
    cell_codes = [c.code for c in zone.cells.order_by(Cell.code).all()]
    pdf = build_zone_label_pdf(zone.code, title, subtitle, cell_codes)
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition(f"zone_{zone.code}.pdf", "inline")},
    )
