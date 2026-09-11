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


def _draw_label_page(
    c, code_value: str, title: str, subtitle: str = "", title_font_size=8,
    max_img_h_ratio=0.55, subtitle_gap_mm=None, top_margin_mm=2, text_gap_mm=3,
):
    """Рисует одну этикетку 58x40мм на текущей странице канваса c —
    используется и для одиночной этикетки, и для пакетной печати (там
    вызывается в цикле с showPage() между этикетками).

    title_font_size по умолчанию 8 (наименование товара может быть длинным
    и разбиваться на 3 строки — крупнее не поместится). Этикетка короба
    печатает короткий текст (сам номер, всегда 1 строка) и просит более
    крупный шрифт и более крупный штрихкод явно (см. вызовы в labels.py) —
    читать номер короба и сканировать штрихкод издалека должно быть проще
    (в т.ч. при смазанной/некачественной печати — крупный штрихкод легче
    отсканировать даже нечетким принтером).

    subtitle_gap_mm — отступ перед подписью под номером; по умолчанию
    (None) равен строчному интервалу заголовка (как было исходно), но при
    крупном title_font_size это дало бы неоправданно большой отступ перед
    мелкой (7pt) подписью — тогда передают отдельное фиксированное
    значение, не зависящее от размера заголовка.

    top_margin_mm/text_gap_mm — отступ над штрихкодом и между штрихкодом и
    текстом; по умолчанию как было исходно (2мм/3мм), но при увеличенном
    штрихкоде (короб) их можно слегка ужать, чтобы освободить место для
    текста снизу без уменьшения самого штрихкода."""
    png_bytes = generate_barcode_png_bytes(code_value)
    img = ImageReader(io.BytesIO(png_bytes))
    img_w, img_h = img.getSize()

    max_img_w = LABEL_WIDTH - 4 * mm
    max_img_h = LABEL_HEIGHT * max_img_h_ratio
    scale = min(max_img_w / img_w, max_img_h / img_h)
    draw_w = img_w * scale
    draw_h = img_h * scale
    x = (LABEL_WIDTH - draw_w) / 2
    y = LABEL_HEIGHT - draw_h - top_margin_mm * mm

    c.drawImage(img, x, y, width=draw_w, height=draw_h, mask="auto")

    text_top = y - text_gap_mm * mm
    c.setFont(FONT_BOLD, title_font_size)

    wrapped = textwrap.wrap(title, width=32) or [""]
    wrapped = wrapped[:3]
    # Пропорционально исходному соотношению 8pt -> 3.4мм между строк, чтобы
    # более крупный шрифт (короб) не наезжал строка на строку.
    line_height = title_font_size * 0.425 * mm
    ty = text_top
    for line in wrapped:
        ty -= line_height
        c.drawCentredString(LABEL_WIDTH / 2, ty, line)

    if subtitle:
        c.setFont(FONT_REGULAR, 7)
        ty -= line_height if subtitle_gap_mm is None else subtitle_gap_mm * mm
        c.drawCentredString(LABEL_WIDTH / 2, ty, subtitle)


def build_label_pdf(
    code_value: str, title: str, subtitle: str = "", title_font_size=8,
    max_img_h_ratio=0.55, subtitle_gap_mm=None, top_margin_mm=2, text_gap_mm=3,
) -> bytes:
    """Строит PDF-этикетку 58x40мм: штрихкод сверху, текст снизу.

    code_value — значение, кодируемое в штрихкод (баркод товара / номер короба / код ячейки).
    title — основная подпись (наименование товара / номер короба / код ячейки).
    subtitle — дополнительная строка (например, артикул).
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(LABEL_WIDTH, LABEL_HEIGHT))
    _draw_label_page(
        c, code_value, title, subtitle,
        title_font_size=title_font_size, max_img_h_ratio=max_img_h_ratio, subtitle_gap_mm=subtitle_gap_mm,
        top_margin_mm=top_margin_mm, text_gap_mm=text_gap_mm,
    )
    c.showPage()
    c.save()
    return buffer.getvalue()


def build_labels_batch_pdf(
    entries, title_font_size=8, max_img_h_ratio=0.55, subtitle_gap_mm=None, top_margin_mm=2, text_gap_mm=3,
) -> bytes:
    """Строит один PDF из нескольких этикеток 58x40мм подряд (одна на
    страницу) — для печати сразу целой партии, например, только что
    массово созданных коробов. entries — список (code_value, title, subtitle)."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(LABEL_WIDTH, LABEL_HEIGHT))
    for code_value, title, subtitle in entries:
        _draw_label_page(
            c, code_value, title, subtitle,
            title_font_size=title_font_size, max_img_h_ratio=max_img_h_ratio, subtitle_gap_mm=subtitle_gap_mm,
            top_margin_mm=top_margin_mm, text_gap_mm=text_gap_mm,
        )
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
