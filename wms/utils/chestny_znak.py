import re

# Код Честного Знака — это GS1 DataMatrix: применяемый идентификатор "01"
# (GTIN товара, всегда 14 цифр) идет первым, за ним — применяемый
# идентификатор "21" (уникальный серийный номер) и служебные крипто-поля
# (91/92/93). Разделители между полями переменной длины сканер может
# передавать по-разному (символ GS \x1D, круглые скобки в "человеческом"
# режиме, либо вообще без разделителя) — но сам GTIN всегда идет сразу
# после "01" и имеет фиксированную длину, поэтому его можно надежно
# вытащить независимо от разделителя.
_GTIN_BRACKETED_RE = re.compile(r"\(01\)(\d{14})")
_GTIN_RAW_RE = re.compile(r"^01(\d{14})")


def extract_gtin(chestny_znak: str) -> str | None:
    """Достает GTIN (14 цифр) из кода Честного Знака, если формат
    распознан. Возвращает None, если код не начинается с ожидаемого
    применяемого идентификатора "01" — тогда товар нужно выбрать вручную."""
    code = chestny_znak.strip()
    match = _GTIN_BRACKETED_RE.search(code)
    if match:
        return match.group(1)
    match = _GTIN_RAW_RE.match(code)
    if match:
        return match.group(1)
    return None


def gtin_barcode_candidates(gtin: str):
    """GTIN-14 сводится к более коротким форматам штрихкода отбрасыванием
    ведущих нулей (EAN-13 из 13 цифр, UPC-A из 12) — обычный штрихкод
    товара в номенклатуре мог быть заведен в любом из этих форматов."""
    candidates = [gtin]
    stripped = gtin.lstrip("0") or "0"
    if stripped not in candidates:
        candidates.append(stripped)
    for width in (13, 12):
        padded = stripped.zfill(width) if len(stripped) <= width else None
        if padded and padded not in candidates:
            candidates.append(padded)
    return candidates
