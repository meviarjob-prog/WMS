#!/usr/bin/env python3
"""Переносит данные WMS из текущей SQLite-базы в Postgres.

Использование (запускать с остановленным сервисом WMS — `systemctl stop
wms` — чтобы за время переноса в SQLite не появилось новых данных, которые
перенос не увидит):

    python deploy/migrate_to_postgres.py postgresql://user:pass@host:5432/wms

Источник по умолчанию — instance/wms.db рядом с проектом (тот же файл,
что использует сам WMS); переопределить можно вторым аргументом или
переменной окружения WMS_SOURCE_DATABASE_URL:

    python deploy/migrate_to_postgres.py postgresql://... sqlite:////opt/wms/instance/wms.db

Что делает:
1. Создает в Postgres все таблицы по тем же моделям SQLAlchemy, что и сам
   WMS (db.metadata.create_all) — схема гарантированно совпадает с кодом,
   а не с чьей-то ручной DDL-копией.
2. Построчно копирует данные из каждой таблицы SQLite в Postgres, в
   порядке, учитывающем внешние ключи (иначе вставка с чужим id упадет).
3. Сдвигает Postgres-последовательности (SERIAL) на максимальный
   перенесенный id — без этого следующая вставка через само приложение
   попыталась бы переиспользовать уже занятый id и упала на уникальном
   ограничении первичного ключа.

После переноса: проверьте данные (например, число строк в счетах на
дашборде), затем пропишите WMS_DATABASE_URL=postgresql://... в
/etc/wms.env на сервере и `systemctl restart wms`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, func, select, text  # noqa: E402

from wms import create_app  # noqa: E402
from wms.config import Config  # noqa: E402
from wms.extensions import db as _db  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    dest_url = sys.argv[1]
    source_url = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("WMS_SOURCE_DATABASE_URL")

    if not dest_url.startswith("postgresql"):
        print("!! Первый аргумент должен быть Postgres-адресом (postgresql://...)", file=sys.stderr)
        sys.exit(1)

    class SourceConfig(Config):
        if source_url:
            SQLALCHEMY_DATABASE_URI = source_url

    # create_app() заодно прогоняет источник через db.create_all()/_ensure_columns() —
    # гарантирует, что копируем уже из полностью актуальной по схеме SQLite-базы.
    app = create_app(SourceConfig)
    with app.app_context():
        source_engine = _db.engine
        metadata = _db.metadata

        print(f"Источник:   {source_engine.url}")
        print(f"Назначение: {dest_url}")
        print()

        dest_engine = create_engine(dest_url)
        metadata.create_all(dest_engine)

        tables = metadata.sorted_tables  # порядок уже учитывает внешние ключи

        with source_engine.connect() as src_conn, dest_engine.begin() as dest_conn:
            for table in tables:
                rows = [dict(row._mapping) for row in src_conn.execute(select(table))]
                if not rows:
                    print(f"  {table.name}: 0 строк")
                    continue

                dest_conn.execute(table.insert(), rows)
                print(f"  {table.name}: {len(rows)} строк")

                if "id" in table.c:
                    max_id = dest_conn.execute(select(func.max(table.c.id))).scalar()
                    if max_id is not None:
                        dest_conn.execute(
                            text("SELECT setval(pg_get_serial_sequence(:table, 'id'), :max_id)"),
                            {"table": table.name, "max_id": max_id},
                        )

        print()
        print("Готово. Проверьте данные в Postgres, затем переключите WMS_DATABASE_URL и перезапустите сервис.")


if __name__ == "__main__":
    main()
