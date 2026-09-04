import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
