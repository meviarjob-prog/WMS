import os
import secrets

from flask import Flask, redirect, request, url_for
from flask_login import current_user
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config, INSTANCE_DIR
from .extensions import db, login_manager
from .paths import resource_dir


_sqlite_functions_registered = False


def _ensure_columns():
    """db.create_all() создает только отсутствующие ТАБЛИЦЫ — если в модель
    существующей таблицы добавили новое поле, на уже работающем сервере (где
    таблица уже есть, но без этой колонки) оно само не появится, и первый же
    запрос к нему упадет с "no such column". Здесь по каждой модели сверяем
    колонки с тем, что реально есть в БД, и недостающие добавляем ALTER TABLE
    (полноценный Alembic для проекта такого размера избыточен)."""
    inspector = inspect(db.engine)
    for table in db.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(db.engine.dialect)
            try:
                with db.engine.begin() as conn:
                    conn.execute(
                        text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}')
                    )
                print(f"[schema] Добавлена колонка {table.name}.{column.name}")
                if table.name == "movement_documents" and column.name == "received_at":
                    # До этой версии перемещение засчитывалось в план отгрузок
                    # сразу по завершении, отдельного подтверждения приемки не
                    # было. Если считать все уже завершенные документы
                    # "неполученными" (received_at пуст), кнопка "Принято на
                    # складе" на них задвоила бы уже учтенное выполнение плана.
                    # Поэтому именно в момент появления колонки (то есть один
                    # раз, при обновлении с более старой версии) закрываем ее
                    # задним числом для всего, что уже было завершено.
                    with db.engine.begin() as conn:
                        conn.execute(
                            text(
                                "UPDATE movement_documents SET received_at = completed_at "
                                "WHERE status = 'completed' AND received_at IS NULL"
                            )
                        )
                    print("[schema] movement_documents.received_at заполнен для уже завершенных документов")
            except Exception as exc:  # noqa: BLE001
                print(f"[schema] Не удалось добавить {table.name}.{column.name}: {exc}")


def _register_sqlite_tuning():
    """SQLite-специфичные настройки:
    - LOWER/UPPER на Python-реализации (сравнение LIKE/ILIKE по умолчанию
      регистронезависимо только для ASCII, кириллица иначе не находится);
    - WAL-режим и busy_timeout — чтобы несколько пользователей одновременно
      (несколько ПК и телефонов) не ловили "database is locked", а запись
      просто немного подождала своей очереди вместо мгновенной ошибки.

    Слушатель "connect" глобальный (на весь процесс, а не на конкретный
    Engine) — если в этом же процессе когда-нибудь подключится не-SQLite
    БД (Postgres), dbapi_connection у нее не будет иметь create_function,
    и это же отличие используем, чтобы не выполнять на ней PRAGMA (там их
    нет и это синтаксическая ошибка) и не регистрировать функции."""
    global _sqlite_functions_registered
    if _sqlite_functions_registered:
        return
    _sqlite_functions_registered = True

    @event.listens_for(Engine, "connect")
    def _on_connect(dbapi_connection, connection_record):  # noqa: ANN001
        if not hasattr(dbapi_connection, "create_function"):
            return  # не SQLite (например, Postgres) — PRAGMA/функции здесь не применимы

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
    from .blueprints.inventory import bp as inventory_bp
    from .blueprints.boxes import bp as boxes_bp
    from .blueprints.labels import bp as labels_bp
    from .blueprints.reports import bp as reports_bp
    from .blueprints.production import bp as production_bp
    from .blueprints.api import bp as api_bp
    from .blueprints.shipment_plan import bp as shipment_plan_bp
    from .blueprints.integration_1c import bp as integration_1c_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(warehouses_bp, url_prefix="/warehouses")
    app.register_blueprint(nomenclature_bp, url_prefix="/nomenclature")
    app.register_blueprint(receiving_bp, url_prefix="/receiving")
    app.register_blueprint(placement_bp, url_prefix="/placement")
    app.register_blueprint(movement_bp, url_prefix="/movement")
    app.register_blueprint(inventory_bp, url_prefix="/inventory")
    app.register_blueprint(boxes_bp, url_prefix="/boxes")
    app.register_blueprint(labels_bp, url_prefix="/labels")
    app.register_blueprint(reports_bp, url_prefix="/reports")
    app.register_blueprint(production_bp, url_prefix="/production")
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(shipment_plan_bp, url_prefix="/shipment-plan")
    app.register_blueprint(integration_1c_bp, url_prefix="/integrations/1c")

    with app.app_context():
        from . import models  # noqa: F401
        from .utils.categorize import bootstrap_categories

        db.create_all()
        _ensure_columns()
        _bootstrap_admin()
        bootstrap_categories()

    @login_manager.user_loader
    def load_user(user_id):
        from .models import User

        return User.query.get(int(user_id))

    @app.before_request
    def require_login():
        from .blueprints.integration_1c import API_1C_PUBLIC_ENDPOINTS

        if request.endpoint is None:
            return None
        if (
            request.endpoint == "static"
            or request.endpoint.startswith("auth.")
            or request.endpoint in API_1C_PUBLIC_ENDPOINTS
        ):
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

        from .models import CELL_CAPACITY

        return {"current_year": datetime.now().year, "CELL_CAPACITY": CELL_CAPACITY}

    return app
