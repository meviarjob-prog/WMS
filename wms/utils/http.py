import re
import urllib.parse

_ASCII_FALLBACK_RE = re.compile(r"[^A-Za-z0-9\-\._]+")


def content_disposition(filename: str, disposition: str = "attachment") -> str:
    """Строит корректный заголовок Content-Disposition для не-ASCII имен файлов.

    HTTP-заголовки должны быть latin-1, поэтому кириллица (например,
    артикул товара или код ячейки, введенные вручную) не может идти прямо
    в filename= — иначе сервер падает с UnicodeEncodeError. Даем ASCII-запасной
    вариант в filename= и полное имя (в т.ч. кириллицу) в filename*=UTF-8''...
    """
    ascii_name = _ASCII_FALLBACK_RE.sub("_", filename).strip("_") or "file"
    quoted = urllib.parse.quote(filename, safe="")
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"
