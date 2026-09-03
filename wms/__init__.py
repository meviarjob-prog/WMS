import os

from flask import Flask
from sqlalchemy import event
from sqlalchemy.engine import Engine

from .config import Config, INSTANCE_DIR
from .extensions import db


_sqlite_functions_registered = False


def _register_unicode_sqlite_functions():
    """SQLite сравнивает LIKE/ILIKE без учета регистра только для ASCII.
    Подменяем LOWER/UPPER на Python-реализацию, чтобы поиск "содержит"
    корректно работал с кириллицей."""
    global _sqlite_functions_registered
    if _sqlite_functions_registered:
        return
    _sqlite_functions_registered = True

    @event.listens_for(Engine, "connect")
    def _on_connect(dbapi_connection, connection_record):  # noqa: ANN001
        if hasattr(dbapi_connection, "create_function"):
            dbapi_connection.create_function(
                "LOWER", 1, lambda s: s.lower() if s is not None else None
            )
            dbapi_connection.create_function(
                "UPPER", 1, lambda s: s.upper() if s is not None else None
            )


def create_app(config_class=Config):
    os.makedirs(INSTANCE_DIR, exist_ok=True)

    _register_unicode_sqlite_functions()

    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)

    from .blueprints.main import bp as main_bp
    from .blueprints.warehouses import bp as warehouses_bp
    from .blueprints.nomenclature import bp as nomenclature_bp
    from .blueprints.receiving import bp as receiving_bp
    from .blueprints.placement import bp as placement_bp
    from .blueprints.movement import bp as movement_bp
    from .blueprints.labels import bp as labels_bp
    from .blueprints.reports import bp as reports_bp
    from .blueprints.api import bp as api_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(warehouses_bp, url_prefix="/warehouses")
    app.register_blueprint(nomenclature_bp, url_prefix="/nomenclature")
    app.register_blueprint(receiving_bp, url_prefix="/receiving")
    app.register_blueprint(placement_bp, url_prefix="/placement")
    app.register_blueprint(movement_bp, url_prefix="/movement")
    app.register_blueprint(labels_bp, url_prefix="/labels")
    app.register_blueprint(reports_bp, url_prefix="/reports")
    app.register_blueprint(api_bp, url_prefix="/api")

    with app.app_context():
        from . import models  # noqa: F401

        db.create_all()

    @app.context_processor
    def inject_globals():
        from datetime import datetime

        return {"current_year": datetime.now().year}

    return app
