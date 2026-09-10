"""Разбор приходной накладной из 1С (лист Excel, печатная форма вида
"Приходная накладная № 1706 от 10 сентября 2026 г."). Точный формат
файла заранее не проверен на реальной выгрузке — как и в
shipment_plan_import.py, ищем по смыслу (заголовки "Код"/"Товары"/
"Количество", строка "Поставщик:"), а не по фиксированным номерам
строк/колонок, чтобы мелкие сдвиги в выгрузке не ломали разбор.

1С очень часто выгружает "в Excel" на самом деле HTML-таблицей с
расширением .xls/.xlsx (реальный OOXML-файл openpyxl не открывает вообще
и падает не InvoiceParseError, а низкоуровневым исключением) — поэтому
если файл не похож на настоящий .xlsx (нет ZIP-сигнатуры), пробуем
разобрать его как HTML-таблицу тем же кодом поиска по смыслу, через
маленькую обертку _ArrayWorksheet с интерфейсом, как у листа openpyxl.

Если реальный файл окажется устроен иначе — здесь единственное место,
которое нужно будет поправить."""

import io
import re
from html.parser import HTMLParser

from openpyxl import load_workbook

_TITLE_RE = re.compile(r"приходная\s+накладная\s*№\s*(\S+)\s*от\s*([^гГ]+?)\s*г\.?", re.IGNORECASE)
_INN_RE = re.compile(r"инн[:\s]*([0-9]{10,12})", re.IGNORECASE)
_PHONE_RE = re.compile(r"тел\.?:?\s*([+0-9][0-9()\-\s]{5,}[0-9])", re.IGNORECASE)

_HEADER_CODE_MARKERS = ("код",)
_HEADER_NAME_MARKERS = ("товар",)
_HEADER_QTY_MARKERS = ("количество", "кол-во")
_STOP_MARKERS = ("итого", "всего наименований")


def _norm(value):
    return str(value).strip() if value is not None else ""


class InvoiceParseError(Exception):
    """Файл не похож на приходную накладную в ожидаемом виде — сообщение
    предназначено для показа пользователю (flash), не только в лог."""


class ParsedInvoice:
    def __init__(self, invoice_number, invoice_date_text, supplier_name, supplier_inn, supplier_phone):
        self.invoice_number = invoice_number
        self.invoice_date_text = invoice_date_text
        self.supplier_name = supplier_name
        self.supplier_inn = supplier_inn
        self.supplier_phone = supplier_phone
        self.rows = []  # list[dict]: code, name, qty


class _ArrayCell:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class _ArrayWorksheet:
    """Обертка над обычной таблицей (список списков строк) с тем же
    интерфейсом (.max_row/.max_column/.cell(row=, column=).value), что и у
    листа openpyxl — чтобы весь код поиска по смыслу ниже работал
    одинаково и для настоящего .xlsx, и для HTML-таблицы 1С."""

    def __init__(self, rows):
        self._rows = rows
        self.max_row = len(rows)
        self.max_column = max((len(r) for r in rows), default=0)

    def cell(self, row, column):
        r, c = row - 1, column - 1
        if 0 <= r < len(self._rows) and 0 <= c < len(self._rows[r]):
            return _ArrayCell(self._rows[r][c])
        return _ArrayCell(None)


class _HtmlTableExtractor(HTMLParser):
    """Извлекает все <table> из HTML в список таблиц (каждая — список строк,
    каждая строка — список текстов ячеек). Вложенные таблицы внутри ячейки
    попадают в общий список отдельным элементом — для наших целей (взять
    самую большую таблицу целиком) это не мешает."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._table_stack = []
        self._row = None
        self._cell_parts = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table_stack.append([])
        elif tag == "tr" and self._table_stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell_parts = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell_parts is not None:
            self._row.append("".join(self._cell_parts).strip())
            self._cell_parts = None
        elif tag == "tr" and self._row is not None:
            self._table_stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._table_stack:
            self.tables.append(self._table_stack.pop())

    def handle_data(self, data):
        if self._cell_parts is not None:
            self._cell_parts.append(data)


def _looks_like_zip(data: bytes) -> bool:
    return data[:2] == b"PK"


def _decode_bytes(data: bytes) -> str:
    # 1C чаще всего отдает такие "HTML-под-видом-Excel" файлы либо в
    # UTF-8, либо в cp1251 (стандартная для старых версий 1С кодировка).
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _parse_html_worksheet(data: bytes):
    text = _decode_bytes(data)
    if "<table" not in text.lower():
        return None
    parser = _HtmlTableExtractor()
    try:
        parser.feed(text)
    except Exception:  # noqa: BLE001 — на входе произвольный внешний файл
        return None
    if not parser.tables:
        return None
    # Печатная форма 1С обычно оборачивает всю накладную в одну таблицу —
    # берем самую большую по числу строк, а не первую попавшуюся (первой
    # может идти служебная/пустая обертка).
    rows = max(parser.tables, key=len)
    return _ArrayWorksheet(rows)


def _load_worksheet(file_stream):
    """Возвращает объект с интерфейсом листа openpyxl — из настоящего
    .xlsx, либо (если это на самом деле HTML/SpreadsheetML под видом
    Excel — частая особенность выгрузки "в Excel" из 1С) из HTML-таблицы.
    Бросает InvoiceParseError с понятным текстом, если файл не подошел ни
    под один вариант — вместо необработанного исключения наружу."""
    data = file_stream.read()
    if _looks_like_zip(data):
        try:
            wb = load_workbook(io.BytesIO(data), data_only=True)
        except Exception as exc:  # noqa: BLE001 — превращаем в понятную ошибку
            raise InvoiceParseError(f"Не удалось открыть файл как Excel: {exc}") from exc
        return wb.worksheets[0]

    ws = _parse_html_worksheet(data)
    if ws is not None:
        return ws

    raise InvoiceParseError(
        "Файл не похож ни на настоящий .xlsx, ни на HTML-таблицу Excel — "
        "попробуйте пересохранить накладную из 1С и загрузить заново"
    )


def _find_title(ws, max_scan_rows=15):
    for r in range(1, min(ws.max_row, max_scan_rows) + 1):
        for c in range(1, ws.max_column + 1):
            text = _norm(ws.cell(row=r, column=c).value)
            if not text:
                continue
            match = _TITLE_RE.search(text)
            if match:
                return match.group(1).strip(), match.group(2).strip()
    return None, None


def _find_supplier(ws, max_scan_rows=15):
    for r in range(1, min(ws.max_row, max_scan_rows) + 1):
        for c in range(1, ws.max_column + 1):
            text = _norm(ws.cell(row=r, column=c).value)
            if not text or "поставщик" not in text.lower():
                continue
            # Реквизиты могут быть в этой же ячейке (после "Поставщик:") или
            # в соседней справа — берем то, что длиннее осмысленного текста.
            candidate = text.split(":", 1)[1].strip() if ":" in text else ""
            if len(candidate) < 3 and c + 1 <= ws.max_column:
                candidate = _norm(ws.cell(row=r, column=c + 1).value)
            if not candidate:
                continue
            inn_match = _INN_RE.search(candidate)
            phone_match = _PHONE_RE.search(candidate)
            name = candidate
            if inn_match:
                name = candidate[: inn_match.start()]
            name = re.split(r",\s*ИНН", name, flags=re.IGNORECASE)[0].strip().strip(",").strip()
            return (
                name or candidate,
                inn_match.group(1) if inn_match else None,
                phone_match.group(1).strip() if phone_match else None,
            )
    return None, None, None


def _find_header_row(ws, max_scan_rows=25):
    """Возвращает (row, code_col, name_col, qty_col)."""
    max_row = min(ws.max_row, max_scan_rows)
    for r in range(1, max_row + 1):
        code_col = name_col = qty_col = None
        for c in range(1, ws.max_column + 1):
            text = _norm(ws.cell(row=r, column=c).value).lower()
            if not text:
                continue
            if code_col is None and any(m in text for m in _HEADER_CODE_MARKERS):
                code_col = c
            if name_col is None and any(m in text for m in _HEADER_NAME_MARKERS):
                name_col = c
            if qty_col is None and any(m in text for m in _HEADER_QTY_MARKERS):
                qty_col = c
        if code_col and name_col and qty_col:
            return r, code_col, name_col, qty_col
    return None, None, None, None


def _to_qty(value):
    if value is None or value == "":
        return None
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return None
    return qty if qty > 0 else None


def parse_invoice(file_stream):
    """Разбирает первый лист файла (настоящий .xlsx или HTML-таблицу под
    видом Excel — см. _load_worksheet). Бросает InvoiceParseError с
    понятным для пользователя текстом при любой проблеме с файлом —
    вместо того чтобы упасть необработанным исключением или молча создать
    пустую/неполную приемку."""
    ws = _load_worksheet(file_stream)

    invoice_number, invoice_date_text = _find_title(ws)
    if not invoice_number:
        raise InvoiceParseError(
            "Не удалось найти в файле номер и дату накладной "
            "(строка вида «Приходная накладная № ... от ... г.»)"
        )

    supplier_name, supplier_inn, supplier_phone = _find_supplier(ws)
    if not supplier_name:
        raise InvoiceParseError("Не удалось найти в файле строку «Поставщик:»")

    header_row, code_col, name_col, qty_col = _find_header_row(ws)
    if header_row is None:
        raise InvoiceParseError(
            "Не удалось найти таблицу товаров (шапку со столбцами «Код», "
            "«Товары», «Количество»)"
        )

    invoice = ParsedInvoice(invoice_number, invoice_date_text, supplier_name, supplier_inn, supplier_phone)

    for r in range(header_row + 1, ws.max_row + 1):
        name = _norm(ws.cell(row=r, column=name_col).value)
        if not name:
            continue
        if any(marker in name.lower() for marker in _STOP_MARKERS):
            break
        code = _norm(ws.cell(row=r, column=code_col).value)
        qty = _to_qty(ws.cell(row=r, column=qty_col).value)
        if qty is None:
            continue
        invoice.rows.append({"code": code, "name": name, "qty": qty})

    if not invoice.rows:
        raise InvoiceParseError("В файле не нашлось ни одной строки с товаром и количеством")

    return invoice
