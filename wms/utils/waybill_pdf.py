import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .labels_pdf import FONT_BOLD, FONT_REGULAR

_TITLE_STYLE = ParagraphStyle("waybill-title", fontName=FONT_BOLD, fontSize=15, spaceAfter=3 * mm)
_META_STYLE = ParagraphStyle("waybill-meta", fontName=FONT_REGULAR, fontSize=10, leading=13)
_HEAD_STYLE = ParagraphStyle("waybill-head", fontName=FONT_BOLD, fontSize=9, textColor=colors.black)
_CELL_STYLE = ParagraphStyle("waybill-cell", fontName=FONT_REGULAR, fontSize=9)
_EMPTY_STYLE = ParagraphStyle("waybill-empty", fontName=FONT_REGULAR, fontSize=10, textColor=colors.grey)


def _fmt_qty(qty):
    if qty == int(qty):
        return str(int(qty))
    return f"{qty:.2f}".rstrip("0").rstrip(".")


def _items_by_nomenclature(document):
    """Суммирует содержимое всех коробов документа по товару — накладная
    отражает физическое перемещение целиком, а не короб за коробом."""
    qty_by_item = {}
    for line in document.lines:
        for box_item in line.box.items:
            qty_by_item[box_item.nomenclature] = (
                qty_by_item.get(box_item.nomenclature, 0) + box_item.qty
            )
    return sorted(qty_by_item.items(), key=lambda pair: pair[0].name)


def build_movement_waybills_pdf(documents) -> bytes:
    """Строит накладные на перемещение — по одной странице(-ам) на документ:
    номер и дата перемещения в шапке, штрихкод/наименование/количество
    (просуммированное по всем коробам документа) в табличной части."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        topMargin=18 * mm,
        bottomMargin=15 * mm,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
    )

    story = []
    for index, movement_doc in enumerate(documents):
        story.append(Paragraph(f"Накладная на перемещение № {movement_doc.number}", _TITLE_STYLE))
        date_value = movement_doc.completed_at or movement_doc.created_at
        story.append(Paragraph(f"Дата перемещения: {date_value.strftime('%d.%m.%Y')}", _META_STYLE))
        story.append(
            Paragraph(
                f"Склад-отправитель: {movement_doc.from_warehouse.name}", _META_STYLE
            )
        )
        story.append(
            Paragraph(f"Склад назначения: {movement_doc.to_warehouse.name}", _META_STYLE)
        )
        story.append(Spacer(1, 6 * mm))

        items = _items_by_nomenclature(movement_doc)
        if not items:
            story.append(Paragraph("В перемещении нет товара (короба пусты).", _EMPTY_STYLE))
        else:
            rows = [
                [
                    Paragraph("Штрихкод", _HEAD_STYLE),
                    Paragraph("Наименование", _HEAD_STYLE),
                    Paragraph("Кол-во", _HEAD_STYLE),
                ]
            ]
            total = 0
            for nomenclature, qty in items:
                rows.append(
                    [
                        Paragraph(nomenclature.barcode, _CELL_STYLE),
                        Paragraph(nomenclature.name, _CELL_STYLE),
                        Paragraph(f"{_fmt_qty(qty)} {nomenclature.unit}", _CELL_STYLE),
                    ]
                )
                total += qty
            rows.append(
                [
                    "",
                    Paragraph("Итого:", _HEAD_STYLE),
                    Paragraph(_fmt_qty(total), _HEAD_STYLE),
                ]
            )

            table = Table(rows, colWidths=[35 * mm, 95 * mm, 30 * mm], repeatRows=1)
            table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -2), 0.5, colors.grey),
                        ("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.black),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("ALIGN", (2, 0), (2, -1), "RIGHT"),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ]
                )
            )
            story.append(table)

        if index < len(documents) - 1:
            story.append(PageBreak())

    doc.build(story)
    return buffer.getvalue()
