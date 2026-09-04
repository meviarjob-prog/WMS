import io
import os
import textwrap

from reportlab.lib.pagesizes import A4, mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from ..paths import resource_dir
from .barcodes import generate_barcode_png_bytes

LABEL_WIDTH = 58 * mm
LABEL_HEIGHT = 40 * mm

# Стандартные PDF-шрифты (Helvetica и т.п.) не содержат кириллицу — вместо
# русских букв печатаются "квадраты". Подключаем TrueType-шрифт с кириллицей.
_FONTS_DIR = os.path.join(resource_dir("static"), "fonts")
FONT_REGULAR = "DejaVuSans"
FONT_BOLD = "DejaVuSans-Bold"

pdfmetrics.registerFont(TTFont(FONT_REGULAR, os.path.join(_FONTS_DIR, "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont(FONT_BOLD, os.path.join(_FONTS_DIR, "DejaVuSans-Bold.ttf")))


def build_label_pdf(code_value: str, title: str, subtitle: str = "") -> bytes:
    """Строит PDF-этикетку 58x40мм: штрихкод сверху, текст снизу.

    code_value — значение, кодируемое в штрихкод (баркод товара / номер короба / код ячейки).
    title — основная подпись (наименование товара / номер короба / код ячейки).
    subtitle — дополнительная строка (например, артикул).
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(LABEL_WIDTH, LABEL_HEIGHT))

    png_bytes = generate_barcode_png_bytes(code_value)
    img = ImageReader(io.BytesIO(png_bytes))
    img_w, img_h = img.getSize()

    max_img_w = LABEL_WIDTH - 4 * mm
    max_img_h = LABEL_HEIGHT * 0.55
    scale = min(max_img_w / img_w, max_img_h / img_h)
    draw_w = img_w * scale
    draw_h = img_h * scale
    x = (LABEL_WIDTH - draw_w) / 2
    y = LABEL_HEIGHT - draw_h - 2 * mm

    c.drawImage(img, x, y, width=draw_w, height=draw_h, mask="auto")

    text_top = y - 3 * mm
    c.setFont(FONT_BOLD, 8)

    wrapped = textwrap.wrap(title, width=32) or [""]
    wrapped = wrapped[:3]
    line_height = 3.4 * mm
    ty = text_top
    for line in wrapped:
        ty -= line_height
        c.drawCentredString(LABEL_WIDTH / 2, ty, line)

    if subtitle:
        c.setFont(FONT_REGULAR, 7)
        ty -= line_height
        c.drawCentredString(LABEL_WIDTH / 2, ty, subtitle)

    c.showPage()
    c.save()
    return buffer.getvalue()


def build_zone_label_pdf(code_value: str, title: str, subtitle: str = "", cell_codes=None) -> bytes:
    """Строит крупную A4-этикетку зоны склада: штрихкод, код/название зоны,
    список входящих ячеек — для печати и навешивания на стеллаж/вход в зону.
    """
    width, height = A4
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)

    margin = 20 * mm

    png_bytes = generate_barcode_png_bytes(code_value)
    img = ImageReader(io.BytesIO(png_bytes))
    img_w, img_h = img.getSize()

    max_img_w = width - 2 * margin
    max_img_h = 60 * mm
    scale = min(max_img_w / img_w, max_img_h / img_h)
    draw_w = img_w * scale
    draw_h = img_h * scale
    x = (width - draw_w) / 2
    y = height - margin - draw_h

    c.drawImage(img, x, y, width=draw_w, height=draw_h, mask="auto")

    ty = y - 18 * mm
    c.setFont(FONT_BOLD, 34)
    c.drawCentredString(width / 2, ty, title)

    if subtitle:
        ty -= 12 * mm
        c.setFont(FONT_REGULAR, 16)
        c.drawCentredString(width / 2, ty, subtitle)

    if cell_codes:
        ty -= 14 * mm
        c.setFont(FONT_BOLD, 12)
        c.drawCentredString(width / 2, ty, "Ячейки в зоне:")
        ty -= 8 * mm
        c.setFont(FONT_REGULAR, 11)
        wrapped = textwrap.wrap(", ".join(cell_codes), width=70)
        for line in wrapped:
            c.drawCentredString(width / 2, ty, line)
            ty -= 6 * mm

    c.showPage()
    c.save()
    return buffer.getvalue()
