import io
import textwrap

from reportlab.lib.pagesizes import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from .barcodes import generate_barcode_png_bytes
from .labels_pdf import FONT_BOLD, FONT_REGULAR, LABEL_HEIGHT, LABEL_WIDTH


def _draw_shipping_label(c, *, code_value, box_number, destination_name, recipient_info, sender_name, index, total):
    """Стикер отправления 58x40мм — в отличие от обычной этикетки короба
    (только штрихкод и номер), несет маршрут: куда именно и от кого едет
    короб, плюс кто его должен принять на месте (recipient_info склада
    назначения, настраивается в «Склады и ячейки»)."""
    png_bytes = generate_barcode_png_bytes(code_value)
    img = ImageReader(io.BytesIO(png_bytes))
    img_w, img_h = img.getSize()

    max_img_w = LABEL_WIDTH - 4 * mm
    max_img_h = LABEL_HEIGHT * 0.30
    scale = min(max_img_w / img_w, max_img_h / img_h)
    draw_w = img_w * scale
    draw_h = img_h * scale
    x = (LABEL_WIDTH - draw_w) / 2
    y = LABEL_HEIGHT - draw_h - 2 * mm
    c.drawImage(img, x, y, width=draw_w, height=draw_h, mask="auto")

    ty = y - 2.6 * mm
    c.setFont(FONT_BOLD, 9)
    for line in textwrap.wrap(destination_name, width=28)[:2]:
        ty -= 3.6 * mm
        c.drawCentredString(LABEL_WIDTH / 2, ty, line)

    c.setFont(FONT_REGULAR, 6.3)
    if recipient_info:
        for line in textwrap.wrap(f"Получатель: {recipient_info}", width=42)[:2]:
            ty -= 2.7 * mm
            c.drawCentredString(LABEL_WIDTH / 2, ty, line)

    for line in textwrap.wrap(f"Отправитель: {sender_name}", width=42)[:1]:
        ty -= 2.7 * mm
        c.drawCentredString(LABEL_WIDTH / 2, ty, line)

    ty -= 3 * mm
    c.setFont(FONT_REGULAR, 6.3)
    c.drawCentredString(LABEL_WIDTH / 2, ty, f"{box_number} · короб {index} из {total}")


def build_movement_shipping_labels_pdf(documents) -> bytes:
    """Один стикер на каждый короб из каждого выбранного перемещения —
    количество стикеров по направлению равно количеству коробов в
    документе этого направления."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(LABEL_WIDTH, LABEL_HEIGHT))
    for document in documents:
        lines = list(document.lines)
        total = len(lines)
        for index, line in enumerate(lines, start=1):
            box = line.box
            _draw_shipping_label(
                c,
                code_value=box.barcode_value,
                box_number=box.box_number,
                destination_name=document.to_warehouse.name,
                recipient_info=document.to_warehouse.recipient_info,
                sender_name=document.from_warehouse.name,
                index=index,
                total=total,
            )
            c.showPage()
    c.save()
    return buffer.getvalue()
