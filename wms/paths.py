"""Пути к упакованным вместе с приложением файлам (HTML-шаблоны, статика,
шрифты) — работает как при обычном запуске из исходников, так и внутри
.exe, собранного PyInstaller.

Почему это отдельная функция, а не просто относительный путь от __file__:
при сборке в один .exe (--onefile) PyInstaller распаковывает файлы во
временную папку (sys._MEIPASS) при каждом запуске. Если положить наши
templates/static под тем же именем "wms/...", что и сам Python-пакет wms
(который PyInstaller тоже анализирует), там возникает конфликт имён и
статика с шаблонами внутри собранного .exe попросту не find. Поэтому в
deploy/desktop.spec эти файлы упаковываются под собственным именем
"resources/..." — вот сюда и указывает эта функция при frozen-запуске.
"""

import os
import sys


def resource_dir(name):
    """name — 'static' или 'templates' (как обычно у Flask)."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return os.path.join(base, "resources", name)
    package_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(package_dir, name)
