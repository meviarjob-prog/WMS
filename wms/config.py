import os
import sys


def _detect_base_dir():
    """Папка, рядом с которой хранятся данные (instance/) — то есть корень
    проекта при обычном запуске, но при запуске из .exe, собранного
    PyInstaller (--onefile), sys.executable указывает на сам .exe, а
    __file__ — на временную папку распаковки (sys._MEIPASS), которая
    удаляется при каждом закрытии программы. Если брать её, база данных
    стиралась бы при каждом перезапуске .exe — поэтому в frozen-режиме
    данные храним рядом с .exe."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _detect_base_dir()
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")


def _env_bool(name, default=False):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes") or default


class Config:
    SECRET_KEY = os.environ.get("WMS_SECRET_KEY", "dev-secret-key-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "WMS_DATABASE_URL", "sqlite:///" + os.path.join(INSTANCE_DIR, "wms.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024  # 20 MB, для загрузки Excel

    # За обратным прокси (nginx) с настоящим HTTPS выставляем куки только по
    # HTTPS. Включается на сервере через WMS_FORCE_SECURE_COOKIES=1 — по
    # умолчанию выключено, чтобы не сломать локальный запуск по обычному http.
    SESSION_COOKIE_SECURE = _env_bool("WMS_FORCE_SECURE_COOKIES")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # Работа за обратным прокси (nginx): доверяем заголовкам X-Forwarded-*,
    # чтобы Flask знал, что запрос пришел по HTTPS. Включается на сервере
    # через WMS_BEHIND_PROXY=1.
    BEHIND_PROXY = _env_bool("WMS_BEHIND_PROXY")
