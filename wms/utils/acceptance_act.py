"""Разбор актов приёмки маркетплейсов (xlsx), которые прикладываются при
кнопке «Принято на складе» в перемещении.

Wildberries («Акт приемки товара № …», файл act-income-*.xlsx): одна
строка = одна единица товара, колонки «баркод», «ШК короба» (это наш
собственный номер короба Box.box_number, он же в выгрузке для WB),
«кол-во» (как правило 1). Ozon: акт приёмки содержит грузоместа (ГМ) —
сопоставление ГМ → наш короб запоминается при выгрузке «Состав грузовых
мест» (MovementCargoPlace); колонки ищутся по синонимам заголовков.

Парсер возвращает {ключ_короба: {баркод: количество}}; что за ключ
(номер нашего короба или штрихкод ГМ) — решает вызывающая сторона.
"""

import io
import re
import zipfile
from collections import OrderedDict
from xml.etree.ElementTree import iterparse

from openpyxl import load_workbook

MAX_ACT_BYTES = 10 * 1024 * 1024

_BOX_HEADERS = (
    "шк короба", "штрихкод короба", "номер короба", "короб",
    "шк гм", "штрихкод гм", "шк грузоместа", "штрихкод грузоместа", "номер грузоместа", "грузоместо", "грузовое место", "гм",
)
_BARCODE_HEADERS = ("баркод", "штрихкод товара", "штрихкод", "шк товара", "barcode")
_ARTICLE_HEADERS = ("артикул товара", "артикул", "артикул продавца")
# Ключ позиции в режиме «по артикулу» (Ozon): артикул и, запасным путём,
# баркод той же строки. Префикс не совпадает ни с одним реальным баркодом.
ARTICLE_KEY_PREFIX = "\x00art:"
# Позиция без баркода/артикула («Неопознанный товар» в акте WB): маркетплейс
# её принял, но мы не можем сказать, что это. Считаем отдельно, в состав
# коробов не вносим.
UNIDENTIFIED_KEY = "\x00unidentified"
_QTY_HEADERS = (
    "кол-во", "количество", "кол во", "принято", "принято на склад (шт.)", "принято на склад", "qty",
)


class ActParseError(ValueError):
    pass


def normalize_header(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("ё", "е"))


def normalize_code(value):
    """Штрихкод/номер из ячейки как строка: числа без «.0», без пробелов."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _find_column(headers, names):
    for name in names:
        for idx, header in enumerate(headers):
            if header == name:
                return idx
    return None


def _is_column_numbering_row(row):
    """Строка нумерации колонок под шапкой («1 | 2 | 3 …») — в актах WB она
    есть, и без пропуска превратилась бы в «короб 7 с баркодом 3»."""
    cells = [normalize_code(c) for c in row if normalize_code(c)]
    return len(cells) >= 3 and all(c.isdigit() and len(c) <= 3 for c in cells)


_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
MAX_UNPACKED_BYTES = 200 * 1024 * 1024


_COL_CACHE = {}


def _col_index(ref):
    key = (ref or "").rstrip("0123456789")
    if key not in _COL_CACHE:
        idx = 0
        for ch in key:
            idx = idx * 26 + ord(ch) - 64
        _COL_CACHE[key] = idx - 1
    return _COL_CACHE[key]


def _shared_strings(zf):
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    out = []
    with zf.open("xl/sharedStrings.xml") as fh:
        for _, el in iterparse(fh):
            if el.tag == _NS + "si":
                # без <rPh> (фонетика) — только видимый текст
                out.append("".join(t.text or "" for t in el.iter(_NS + "t") if t not in set(el.iter(_NS + "rPh"))))
                el.clear()
    return out


def _iter_sheet_rows_fast(data, wanted):
    """Быстрое чтение xlsx напрямую из XML: для актов на десятки тысяч строк
    (одна строка = одна единица товара) openpyxl тратит секунды, а нам нужны
    три колонки. Отдаёт по листу генератор строк (tuple значений)."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    if sum(i.file_size for i in zf.infolist()) > MAX_UNPACKED_BYTES:
        raise ActParseError("Файл слишком большой после распаковки")
    shared = _shared_strings(zf)
    sheets = sorted(
        (n for n in zf.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
        key=lambda n: int(re.search(r"(\d+)\.xml", n).group(1)),
    )
    for name in sheets:
        yield _sheet_rows(zf, name, shared, wanted)


def _sheet_rows(zf, name, shared, wanted):
    # wanted — множество нужных колонок; заполняется, когда найдена шапка
    # (до этого читаем все колонки), дальше остальные ячейки пропускаем.
    with zf.open(name) as fh:
        for _, row_el in iterparse(fh):
            if row_el.tag != _NS + "row":
                continue
            cells = {}
            for c in row_el.findall(_NS + "c"):
                col = _col_index(c.get("r"))
                if wanted and col not in wanted:
                    continue
                kind = c.get("t")
                if kind == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter(_NS + "t"))
                else:
                    v = c.find(_NS + "v")
                    if v is None or v.text is None:
                        continue
                    if kind == "s":
                        val = shared[int(v.text)]
                    elif kind in ("str", "e"):
                        val = v.text
                    elif kind == "b":
                        val = v.text == "1"
                    else:
                        num = float(v.text)
                        val = int(num) if num.is_integer() else num
                cells[col] = val
            row_el.clear()
            width = max(cells) + 1 if cells else 0
            yield tuple(cells.get(i) for i in range(width))


def _iter_sheet_rows_openpyxl(data, wanted):
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    for ws in wb.worksheets:
        ws.reset_dimensions()  # в актах WB размер листа в файле указан неверно
        yield ws.iter_rows(values_only=True)


def _parse_rows(rows, wanted, prefer_article=False):
    """rows — итератор строк одного листа. None, если шапки нет."""
    box_col = bc_col = qty_col = art_col = None
    header_found = False
    header_row = 0
    boxes = OrderedDict()
    total = 0
    for i, row in enumerate(rows):
        if not header_found:
            if i >= 40:
                return None
            headers = [normalize_header(c) for c in row]
            box_col = _find_column(headers, _BOX_HEADERS)
            bc_col = _find_column(headers, _BARCODE_HEADERS)
            if box_col is not None and bc_col is not None and box_col != bc_col:
                qty_col = _find_column(headers, _QTY_HEADERS)
                header_found = True
                header_row = i
                if prefer_article:
                    art_col = _find_column(headers, _ARTICLE_HEADERS)
                wanted.update(c for c in (box_col, bc_col, qty_col, art_col) if c is not None)
            continue
        if len(row) <= max(box_col, bc_col):
            continue
        box = normalize_code(row[box_col])
        barcode = normalize_code(row[bc_col])
        article = ""
        if art_col is not None and art_col < len(row):
            article = normalize_code(row[art_col])
        if not box:
            continue
        if i - header_row <= 3 and _is_column_numbering_row(row):
            continue
        if not (barcode or article):
            # строка с коробом и количеством, но без товара
            if qty_col is None or qty_col >= len(row) or row[qty_col] in (None, ""):
                continue
            barcode = UNIDENTIFIED_KEY
        qty = 1.0
        if qty_col is not None and qty_col < len(row) and row[qty_col] not in (None, ""):
            try:
                qty = float(row[qty_col])
            except (TypeError, ValueError):
                continue
        if qty <= 0:
            continue
        key = f"{ARTICLE_KEY_PREFIX}{article}\x1f{barcode}" if article else barcode
        if barcode == UNIDENTIFIED_KEY:
            key = UNIDENTIFIED_KEY
        items = boxes.setdefault(box, {})
        items[key] = items.get(key, 0) + qty
        total += qty
    if not header_found:
        return None
    if not boxes:
        raise ActParseError("В акте не найдено ни одной строки с коробом и баркодом")
    return boxes, total


def unwrap_archive(data):
    """WB отдаёт акт архивом (Acceptance-certificate.zip: act-income-….xlsx,
    подпись, mchd.zip) — принимаем его как есть, без ручной распаковки.
    Для обычного xlsx (он сам zip) возвращает данные без изменений."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return data
    names = zf.namelist()
    if "[Content_Types].xml" in names or "xl/workbook.xml" in names:
        return data
    xlsx = [n for n in names if n.lower().endswith(".xlsx") and not n.startswith("__MACOSX")]
    if not xlsx:
        raise ActParseError("В архиве нет файла акта (.xlsx)")
    xlsx.sort(key=lambda n: (not re.search(r"act|акт", n.lower()), n))
    info = zf.getinfo(xlsx[0])
    if info.file_size > MAX_ACT_BYTES:
        raise ActParseError("Файл акта в архиве слишком большой (больше 10 МБ)")
    return zf.read(xlsx[0])


def parse_act(data, prefer_article=False):
    """Возвращает (boxes, total_qty): boxes = OrderedDict {ключ: {баркод: qty}}.

    Шапка ищется среди первых 40 строк (в акте WB она двухстрочная — над
    ней группа «Фактически принято»): нужна строка, где есть и колонка
    короба, и колонка баркода. prefer_article (Ozon): позиции ключуются
    артикулом из акта (ARTICLE_KEY_PREFIX) — баркод в отчётах Ozon
    теряет/добавляет ведущий ноль, а артикул у нас уже есть в справочнике. Строки читаются потоком и сразу
    суммируются — акт на десятки тысяч строк не держится в памяти."""
    if not data:
        raise ActParseError("Файл пустой")
    if len(data) > MAX_ACT_BYTES:
        raise ActParseError("Файл слишком большой (больше 10 МБ)")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ActParseError("Не удалось прочитать файл — нужен Excel (.xlsx) или архив с актом (.zip)")
    data = unwrap_archive(data)

    for reader in (_iter_sheet_rows_fast, _iter_sheet_rows_openpyxl):
        try:
            wanted = set()
            for rows in reader(data, wanted):
                wanted.clear()
                result = _parse_rows(rows, wanted, prefer_article)
                if result is not None:
                    return result
            break  # файл прочитан, но шапки нет ни на одном листе
        except ActParseError:
            raise
        except Exception:
            continue  # нестандартный xlsx — пробуем следующий способ чтения

    raise ActParseError(
        "Не найдены колонки «ШК короба» (или грузоместо) и «баркод» — "
        "загрузите акт приёмки в том виде, как его выдал маркетплейс"
    )
