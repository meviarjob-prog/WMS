"""Общие fixtures для тестов.

БД — SQLite в памяти (StaticPool держит одно и то же соединение на все
время теста, иначе каждое новое соединение к ":memory:" видело бы пустую
базу). create_app() сам создает таблицы и админа с автопаролем — пароль
нам не нужен, для логина в тестах подставляем user_id прямо в сессию."""

import pytest
from sqlalchemy.pool import StaticPool

from wms import create_app
from wms.config import Config
from wms.extensions import db as _db
from wms.models import User


class TestConfig(Config):
    TESTING = True
    SECRET_KEY = "test-secret-key"
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SQLALCHEMY_ENGINE_OPTIONS = {
        "poolclass": StaticPool,
        "connect_args": {"check_same_thread": False},
    }
    WTF_CSRF_ENABLED = False


@pytest.fixture()
def app():
    application = create_app(TestConfig)
    yield application


@pytest.fixture()
def db(app):
    with app.app_context():
        yield _db


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin_user(db):
    return User.query.filter_by(username="admin").first()


@pytest.fixture()
def client_logged_in(client, admin_user):
    """Тестовый клиент, залогиненный админом — большинству тестов ролевые
    ограничения не важны, а без логина все маршруты редиректят на /login
    (см. require_login в wms/__init__.py)."""
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin_user.id)
        sess["_fresh"] = True
    return client
