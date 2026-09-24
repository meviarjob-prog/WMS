import datetime
import io
import textwrap

from reportlab.lib.pagesizes import mm
from reportlab.pdfgen import canvas

from .labels_pdf import FONT_BOLD, FONT_REGULAR, LABEL_HEIGHT, LABEL_WIDTH

# Поля и их порядок повторяют бумажный бланк поставки (Отправитель /
# Направление / Количество коробов N из Total / Дата поставки / Площадка),
# который раньше заполнялся от руки — теперь печатается сразу заполненным.
_LABEL_FIELD_FONT_SIZE = 9
_LABEL_LINE_GAP = 5 * mm


def _draw_shipping_label(c, *, sender_name, destination_name, box_index, box_count, marketplace_label):
    """Стикер отправления 58x40мм — один на каждый короб перемещения, с
    порядковым номером этого короба в общем количестве (box_index из
    box_count), чтобы на месте приемки можно было по стикерам проверить,
    что приехали все короба поставки."""
    ty = LABEL_HEIGHT - 6 * mm
    c.setFont(FONT_BOLD, _LABEL_FIELD_FONT_SIZE)

    lines = [f"Отправитель: {sender_name}"]
    for line in textwrap.wrap(lines[0], width=30)[:2]:
        c.drawString(3 * mm, ty, line)
        ty -= 4 * mm

    for line in textwrap.wrap(f"Направление: {destination_name}", width=30)[:2]:
        c.drawString(3 * mm, ty, line)
        ty -= 4 * mm

    c.drawString(3 * mm, ty, f"Количество коробов: {box_index} из {box_count}")
    ty -= _LABEL_LINE_GAP

    c.drawString(3 * mm, ty, f"Дата поставки: {datetime.date.today().strftime('%d.%m.%Y')}")
    ty -= _LABEL_LINE_GAP

    c.drawString(3 * mm, ty, f"Площадка: {marketplace_label or '—'}")


def build_movement_shipping_labels_pdf(documents, sender_override=None) -> bytes:
    """Печатает по одному стикеру на каждый короб перемещения — с
    порядковым номером короба в общем количестве коробов документа (см.
    _draw_shipping_label). Маршрут, отправитель, дата и площадка у всех
    стикеров одного документа одинаковые — отличается только "Количество
    коробов: N из ...".

    sender_override — если задан, печатается как "Отправитель" вместо
    названия фактического склада-отправителя, сразу для всех документов
    (единый отправитель на все направления, настраивается в «Настройки»)."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(LABEL_WIDTH, LABEL_HEIGHT))
    for document in documents:
        sender_name = sender_override or document.from_warehouse.name
        lines = list(document.lines)
        box_count = len(lines)
        for index, _line in enumerate(lines, start=1):
            _draw_shipping_label(
                c,
                sender_name=sender_name,
                destination_name=document.to_warehouse.name,
                box_index=index,
                box_count=box_count,
                marketplace_label=document.to_warehouse.marketplace_label(),
            )
            c.showPage()
    c.save()
    return buffer.getvalue()
