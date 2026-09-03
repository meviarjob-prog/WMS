import base64
import io
import re

import barcode
from barcode.writer import ImageWriter

# Code128 допускает практически любые печатные ASCII-символы, но для надежности
# сканирования оставляем буквы, цифры и несколько разделителей.
_SAFE_RE = re.compile(r"[^A-Za-z0-9\-\._/ ]+")


def _sanitize(value: str) -> str:
    value = (value or "").strip()
    value = _SAFE_RE.sub("", value)
    return value or "0"


def generate_barcode_png_bytes(value: str) -> bytes:
    """Генерирует PNG штрихкода Code128 и возвращает байты изображения."""
    code = barcode.get(
        "code128",
        _sanitize(value),
        writer=ImageWriter(),
    )
    buffer = io.BytesIO()
    code.write(
        buffer,
        options={
            "module_height": 12.0,
            "module_width": 0.28,
            "font_size": 8,
            "text_distance": 3,
            "quiet_zone": 2,
            "write_text": True,
        },
    )
    return buffer.getvalue()


def generate_barcode_data_uri(value: str) -> str:
    png_bytes = generate_barcode_png_bytes(value)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"
