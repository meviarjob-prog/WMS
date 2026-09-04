# -*- mode: python ; coding: utf-8 -*-
#
# Собирает desktop.py в один Windows .exe (WMS.exe), включающий сам сервер
# и (если файл cloudflared.exe лежит в корне проекта на момент сборки)
# бинарник Cloudflare Tunnel для автоматической HTTPS-ссылки с камерой.
#
# Как собрать — см. deploy/build_exe.md. Короткая версия:
#   pip install pyinstaller
#   pyinstaller deploy/desktop.spec --distpath dist --workpath build
#
# Запускать команду из корня проекта (там, где run.py, desktop.py).

import os

block_cipher = None

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(SPEC)), ".."))

# ВАЖНО: упаковываем НЕ под именем "wms/..." — так называется сам Python-пакет
# приложения, который PyInstaller тоже анализирует и раскладывает по .exe;
# при совпадении имени и то, и другое конфликтует и наши файлы шаблонов/
# статики теряются при сборке через .spec. Имя "resources/..." выбрано не
# просто так — это ровно то, что ждет wms/paths.py::resource_dir() в
# frozen-режиме. Меняете один — меняйте и другой.
datas = [
    (os.path.join(PROJECT_ROOT, "wms", "templates"), os.path.join("resources", "templates")),
    (os.path.join(PROJECT_ROOT, "wms", "static"), os.path.join("resources", "static")),
]

# python-barcode и reportlab хранят собственные файлы (шрифты, метрики) внутри
# пакета — без этого шага штрихкоды/PDF-этикетки в .exe не сработают.
try:
    from PyInstaller.utils.hooks import collect_data_files

    datas += collect_data_files("barcode")
    datas += collect_data_files("reportlab")
except Exception:
    pass

binaries = []
cloudflared_path = os.path.join(PROJECT_ROOT, "cloudflared.exe")
if os.path.isfile(cloudflared_path):
    binaries.append((cloudflared_path, "."))
else:
    print(
        "!! cloudflared.exe не найден в корне проекта — соберется .exe без "
        "автоматической HTTPS-ссылки (см. deploy/build_exe.md, шаг 2)."
    )

a = Analysis(
    [os.path.join(PROJECT_ROOT, "desktop.py")],
    pathex=[PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WMS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
