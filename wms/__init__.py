import os
import secrets

from flask import Flask, redirect, request, url_for
from flask_login import current_user
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config, INSTANCE_DIR
from .extensions import db, login_manager
from .paths import resource_dir


_sqlite_functions_registered = False


def _register_sqlite_tuning():
    """SQLite-специфичные настройки:
    - LOWER/UPPER на Python-реализации (сравнение LIKE/ILIKE по умолчанию
      регистронезависимо только для ASCII, кириллица иначе не находится);
    - WAL-режим и busy_timeout — чтобы несколько пользователей одновременно
      (несколько ПК и телефонов) не ловили "database is locked", а запись
      просто немного подождала своей очереди вместо мгновенной ошибки.
    """
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
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def _bootstrap_admin():
    """Если пользователей еще нет (первый запуск) — создает администратора
    со случайным паролем и печатает его в консоль. Больше пароль нигде не
    хранится в открытом виде — при необходимости его можно сбросить в
    разделе «Пользователи»."""
    from .models import User

    if User.query.count() > 0:
        return

    password = secrets.token_urlsafe(8)
    admin = User(username="admin", full_name="Администратор", is_admin=True)
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()

    print("=" * 60)
    print("Создан первый пользователь администратора:")
    print("  Логин:  admin")
    print(f"  Пароль: {password}")
    print("Сохраните этот пароль — он больше нигде не показывается.")
    print("Сменить его можно после входа или в разделе «Пользователи».")
    print("=" * 60)


def create_app(config_class=Config):
    os.makedirs(INSTANCE_DIR, exist_ok=True)

    _register_sqlite_tuning()

    app = Flask(
        __name__,
        template_folder=resource_dir("templates"),
        static_folder=resource_dir("static"),
    )
    app.config.from_object(config_class)

    if app.config.get("BEHIND_PROXY"):
        # За nginx: доверяем X-Forwarded-For/-Proto/-Host от ровно одного
        # прокси перед приложением, чтобы Flask видел правильную схему
        # (https) и IP клиента вместо адреса самого nginx.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    login_manager.init_app(app)

    from .blueprints.auth import bp as auth_bp
    from .blueprints.main import bp as main_bp
    from .blueprints.warehouses import bp as warehouses_bp
    from .blueprints.nomenclature import bp as nomenclature_bp
    from .blueprints.receiving import bp as receiving_bp
    from .blueprints.placement import bp as placement_bp
    from .blueprints.movement import bp as movement_bp
    from .blueprints.boxes import bp as boxes_bp
    from .blueprints.labels import bp as labels_bp
    from .blueprints.reports import bp as reports_bp
    from .blueprints.production import bp as production_bp
    from .blueprints.api import bp as api_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(warehouses_bp, url_prefix="/warehouses")
    app.register_blueprint(nomenclature_bp, url_prefix="/nomenclature")
    app.register_blueprint(receiving_bp, url_prefix="/receiving")
    app.register_blueprint(placement_bp, url_prefix="/placement")
    app.register_blueprint(movement_bp, url_prefix="/movement")
    app.register_blueprint(boxes_bp, url_prefix="/boxes")
    app.register_blueprint(labels_bp, url_prefix="/labels")
    app.register_blueprint(reports_bp, url_prefix="/reports")
    app.register_blueprint(production_bp, url_prefix="/production")
    app.register_blueprint(api_bp, url_prefix="/api")

    with app.app_context():
        from . import models  # noqa: F401

        db.create_all()
        _bootstrap_admin()

    @login_manager.user_loader
    def load_user(user_id):
        from .models import User

        return User.query.get(int(user_id))

    @app.before_request
    def require_login():
        if request.endpoint is None:
            return None
        if request.endpoint == "static" or request.endpoint.startswith("auth."):
            return None
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.full_path))
        # Роль "производство" — доступ только к сканированию ЧЗ, ничего
        # больше (даже при прямом вводе адреса другой страницы).
        if current_user.is_production_only() and not request.endpoint.startswith("production."):
            return redirect(url_for("production.index"))
        return None

    @app.context_processor
    def inject_globals():
        from datetime import datetime

        return {"current_year": datetime.now().year}

    return app
