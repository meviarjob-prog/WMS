import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")


class Config:
    SECRET_KEY = os.environ.get("WMS_SECRET_KEY", "dev-secret-key-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "WMS_DATABASE_URL", "sqlite:///" + os.path.join(INSTANCE_DIR, "wms.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024  # 20 MB, для загрузки Excel
