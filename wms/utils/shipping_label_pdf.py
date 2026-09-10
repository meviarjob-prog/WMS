import io
import textwrap

from reportlab.lib.pagesizes import mm
from reportlab.pdfgen import canvas

from .labels_pdf import FONT_BOLD, FONT_REGULAR, LABEL_HEIGHT, LABEL_WIDTH


def _draw_shipping_label(c, *, destination_name, recipient_info, sender_name, box_number=None):
    """Стикер отправления 58x40мм — наклейка на короб для конкретного
    перемещения (маршрут: откуда и куда, кто получатель). Привязан к
    конкретному коробу через box_number — печатается крупно и первым, чтобы
    при проклейке можно было сверить его с номером на самом коробе (на
    короб уже наклеена отдельная «Этикетка короба» с тем же номером и
    штрихкодом, см. labels_pdf.py) и не перепутать стикеры между собой,
    если печатается сразу несколько направлений."""
    ty = LABEL_HEIGHT - 6 * mm

    if box_number:
        c.setFont(FONT_BOLD, 11)
        c.drawCentredString(LABEL_WIDTH / 2, ty, f"Короб: {box_number}")
        ty -= 5.5 * mm

    c.setFont(FONT_BOLD, 12)
    for line in textwrap.wrap(destination_name, width=24)[:2]:
        c.drawCentredString(LABEL_WIDTH / 2, ty, line)
        ty -= 5 * mm

    ty -= 1.5 * mm
    c.setFont(FONT_REGULAR, 9)
    if recipient_info:
        for line in textwrap.wrap(f"Получатель: {recipient_info}", width=30)[:2]:
            c.drawCentredString(LABEL_WIDTH / 2, ty, line)
            ty -= 4 * mm
        ty -= 1.2 * mm

    for line in textwrap.wrap(f"Отправитель: {sender_name}", width=30)[:2]:
        c.drawCentredString(LABEL_WIDTH / 2, ty, line)
        ty -= 4 * mm


def build_movement_shipping_labels_pdf(documents, sender_override=None) -> bytes:
    """Печатает по одному стикеру на каждый короб перемещения — с номером
    ЭТОГО конкретного короба на стикере (см. _draw_shipping_label), чтобы
    при проклейке можно было перепроверить, что стикер клеится на
    правильный короб, а не просто взять любой стикер из пачки одного
    направления. Маршрут и получатель у всех стикеров одного документа
    одинаковые — отличается только строка "Короб: ...".

    sender_override — если задан, печатается как "Отправитель" вместо
    названия фактического склада-отправителя, сразу для всех документов
    (единый отправитель на все направления, настраивается в «Настройки»)."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(LABEL_WIDTH, LABEL_HEIGHT))
    for document in documents:
        sender_name = sender_override or document.from_warehouse.name
        for line in document.lines:
            _draw_shipping_label(
                c,
                destination_name=document.to_warehouse.name,
                recipient_info=document.to_warehouse.recipient_info,
                sender_name=sender_name,
                box_number=line.box.box_number,
            )
            c.showPage()
    c.save()
    return buffer.getvalue()
