"""Разбор файла плана отгрузок (выгрузка из общей таблицы MEVIAR).

Формат листов заранее не фиксирован жестко — заголовок с городами каждый
раз может съехать на строку/колонку, а сама таблица заливается заново раз
в 2 недели под новой датой в названии листа. Поэтому вместо фиксированных
номеров строк/колонок здесь идет поиск по смыслу: строка-заголовок находится
по ячейке "Баркод", дальше колонки-города определяются по тому, что их
подпись не похожа на служебную ("остатки", "отгружен / в пути" и т.п.).
"""

import re
from datetime import date

from openpyxl import load_workbook

_PERIOD_START_RE = re.compile(r"от\s+(\d{1,2})\.(\d{1,2})")


def extract_period_start(sheet_name, today=None):
    """Дата начала периода плана из названия листа ("...от 27.08" -> 27
    августа). Год не указан в файле — берем текущий, а если получившаяся
    дата вышла в будущем больше чем на месяц (переход через Новый год,
    например план от 28.12 гружен уже в январе) — откатываем на год назад."""
    match = _PERIOD_START_RE.search(sheet_name or "")
    if not match:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    today = today or date.today()
    try:
        result = date(today.year, month, day)
    except ValueError:
        return None
    if (result - today).days > 31:
        result = date(today.year - 1, month, day)
    return result

# Подписи служебных/общих колонок (не города): и суб-колонки под городом
# (остаток/факт маркетплейса), и общие метаданные товара, которые могут
# встретиться правее штрихкода (GTIN, кол-во в коробе, план продаж и т.п.).
# Если подпись колонки не содержит ни одного из этих кусков — считаем ее
# названием нового города. Список специально с запасом (шире, чем нужно
# для текущего файла) — раз в 2 недели заливается новый файл, и лишняя
# устойчивость к формулировкам не помешает.
_SUBCOLUMN_MARKERS = (
    "остат", "отгруж", "путь", "факт", "план", "%", "gtin", "sku",
    "коробе", "производств", "хватает", "всего", "раскладк", "продаж",
    "дней", "поставк", "склад", "приорит",
)

_SHEET_ALIASES = {
    "ozon": ("озон",),
    "wb": ("вб", "wb"),
}


def _norm(value):
    return str(value).strip() if value is not None else ""


def _find_plan_sheet(wb, marketplace):
    markers = _SHEET_ALIASES[marketplace]
    for name in wb.sheetnames:
        lower = name.lower()
        if "распределение" in lower and any(m in lower for m in markers):
            return name
    return None


def _find_header_row(ws, max_scan_rows=40):
    """Возвращает (row_idx, barcode_col) — строку и колонку с "Баркод"."""
    max_row = min(ws.max_row, max_scan_rows)
    for r in range(1, max_row + 1):
        for c in range(1, ws.max_column + 1):
            v = _norm(ws.cell(row=r, column=c).value).lower()
            if "баркод" in v:
                return r, c
    return None, None


def _find_label_col(ws, header_row, barcode_col, keyword):
    for c in range(1, barcode_col):
        v = _norm(ws.cell(row=header_row, column=c).value).lower()
        if keyword in v:
            return c
    return None


def _find_city_columns(ws, header_row, barcode_col):
    """[(plan_col, city_name, fact_col), ...] — plan_col это первая колонка
    группы города (план по количеству); fact_col — колонка "отгружен / в
    пути" из той же группы (уже фактически отгружено на момент выгрузки
    плана), если она есть, иначе None. Остальные служебные колонки группы
    (например "остатки" у ОЗОН) пропускаем — они не нужны."""
    raw = []
    for c in range(barcode_col + 1, ws.max_column + 1):
        text = _norm(ws.cell(row=header_row, column=c).value)
        raw.append((c, text))

    cities = []
    i = 0
    while i < len(raw):
        col, text = raw[i]
        if not text:
            i += 1
            continue
        lower = text.lower()
        if any(marker in lower for marker in _SUBCOLUMN_MARKERS):
            i += 1
            continue
        # Нашли новую колонку-город — ищем "отгружен / в пути" среди
        # следующих колонок этой же группы, пока не встретим следующий город.
        fact_col = None
        j = i + 1
        while j < len(raw):
            next_col, next_text = raw[j]
            next_lower = next_text.lower()
            if next_text and not any(m in next_lower for m in _SUBCOLUMN_MARKERS):
                break  # это уже следующий город
            if "отгруж" in next_lower or "путь" in next_lower:
                fact_col = next_col
            j += 1
        cities.append((col, text, fact_col))
        i += 1
    return cities


def _to_barcode_str(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _to_qty(value):
    if value is None or value == "":
        return None
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return None
    return qty if qty > 0 else None


def _to_fact_qty(value):
    """В отличие от _to_qty, ноль/пусто — это законное "еще не отгружено",
    а не повод пропустить город (в отличие от плана, где qty=0 не создает
    строку вовсе)."""
    if value is None or value == "":
        return 0.0
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return 0.0
    return qty if qty > 0 else 0.0


class ParsedPlan:
    def __init__(self, sheet_name):
        self.sheet_name = sheet_name
        self.cities = []  # list[str] в порядке появления
        self.rows = []  # list[dict]: barcode, article, size, city, qty, fact


def parse_plan_sheet(file_stream, marketplace):
    """Возвращает ParsedPlan либо None, если подходящий лист не найден."""
    wb = load_workbook(file_stream, data_only=True)
    sheet_name = _find_plan_sheet(wb, marketplace)
    if not sheet_name:
        return None

    ws = wb[sheet_name]
    header_row, barcode_col = _find_header_row(ws)
    if header_row is None:
        return None

    article_col = _find_label_col(ws, header_row, barcode_col, "артикул")
    size_col = _find_label_col(ws, header_row, barcode_col, "размер")
    city_columns = _find_city_columns(ws, header_row, barcode_col)

    plan = ParsedPlan(sheet_name)
    plan.cities = [name for _, name, _ in city_columns]

    for r in range(header_row + 1, ws.max_row + 1):
        barcode = _to_barcode_str(ws.cell(row=r, column=barcode_col).value)
        if not barcode:
            continue  # строки-подытоги ("ВСЕГО", "КАРДИГАНЫ" и т.п.) без штрихкода

        article = _norm(ws.cell(row=r, column=article_col).value) if article_col else ""
        size = _norm(ws.cell(row=r, column=size_col).value) if size_col else ""

        for col, city, fact_col in city_columns:
            qty = _to_qty(ws.cell(row=r, column=col).value)
            fact = _to_fact_qty(ws.cell(row=r, column=fact_col).value) if fact_col else 0.0
            # Раньше здесь пропускалась вся строка, если план (qty) пуст —
            # но реальный файл в ~1% ячеек показывает факт по городу, для
            # которого план не проставлен (0 или пусто). Пропуск такой
            # строки тихо терял уже отгруженное количество из общей суммы
            # факта (сумма расходилась с "Факт отгружено 14 д." в файле).
            # Плюс раньше факт обрезался по плану той же строки (fact =
            # min(fact, qty)) — из-за той же причины (факт местами больше
            # плана на конкретной паре товар-город) это тоже теряло часть
            # уже отгруженного. remaining_qty() и так не уходит в минус,
            # поэтому обрезать факт не нужно — тянем оба значения как есть.
            if qty is None and fact <= 0:
                continue
            plan.rows.append(
                {
                    "barcode": barcode,
                    "article": article,
                    "size": size,
                    "city": city,
                    "qty": qty or 0.0,
                    "fact": fact,
                }
            )

    return plan
