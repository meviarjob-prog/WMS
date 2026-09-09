import io
import textwrap

from reportlab.lib.pagesizes import mm
from reportlab.pdfgen import canvas

from .labels_pdf import FONT_BOLD, FONT_REGULAR, LABEL_HEIGHT, LABEL_WIDTH


def _draw_shipping_label(c, *, destination_name, recipient_info, sender_name):
    """Стикер отправления 58x40мм — общая наклейка на короб для конкретного
    перемещения (маршрут: откуда и куда, кто получатель), НЕ привязанная к
    конкретному коробу — все стикеры для одного перемещения одинаковые,
    штрихкод короба на них не печатается (для этого есть отдельная
    «Этикетка короба», см. labels_pdf.py)."""
    ty = LABEL_HEIGHT - 7 * mm

    c.setFont(FONT_BOLD, 12)
    for line in textwrap.wrap(destination_name, width=24)[:2]:
        c.drawCentredString(LABEL_WIDTH / 2, ty, line)
        ty -= 5 * mm

    ty -= 2 * mm
    c.setFont(FONT_REGULAR, 7.5)
    if recipient_info:
        for line in textwrap.wrap(f"Получатель: {recipient_info}", width=36)[:3]:
            c.drawCentredString(LABEL_WIDTH / 2, ty, line)
            ty -= 3.6 * mm
        ty -= 1.5 * mm

    for line in textwrap.wrap(f"Отправитель: {sender_name}", width=36)[:2]:
        c.drawCentredString(LABEL_WIDTH / 2, ty, line)
        ty -= 3.6 * mm


def build_movement_shipping_labels_pdf(documents, sender_override=None) -> bytes:
    """Печатает столько одинаковых стикеров, сколько коробов в документе —
    по одному на каждый короб перемещения, но без привязки к конкретному
    коробу (все стикеры одного документа идентичны: только маршрут и
    получатель, без номера/штрихкода короба).

    sender_override — если задан, печатается как "Отправитель" вместо
    названия фактического склада-отправителя, сразу для всех документов
    (единый отправитель на все направления, настраивается в «Настройки»)."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(LABEL_WIDTH, LABEL_HEIGHT))
    for document in documents:
        box_count = document.lines.count()
        sender_name = sender_override or document.from_warehouse.name
        for _ in range(box_count):
            _draw_shipping_label(
                c,
                destination_name=document.to_warehouse.name,
                recipient_info=document.to_warehouse.recipient_info,
                sender_name=sender_name,
            )
            c.showPage()
    c.save()
    return buffer.getvalue()
