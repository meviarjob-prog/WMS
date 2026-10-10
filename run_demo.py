"""Запуск демо-версии WMS для презентации.

    python3 run_demo.py            # поднять демо на http://localhost:5050
    python3 run_demo.py --reset    # стереть демо-базу и наполнить заново
    python3 run_demo.py --lan      # открыть в локальной сети (показ с телефона)
    python3 run_demo.py --seed-only  # только подготовить базу, сервер не запускать

Демо живет в ОТДЕЛЬНОЙ базе instance/demo.db с вымышленными данными и не
трогает боевую instance/wms.db. Переменные окружения выставляются ДО
импорта приложения, поэтому даже если на машине задан WMS_DATABASE_URL
боевой базы, демо его игнорирует. Выгрузка в Google Таблицу отключена
(ключ намеренно не указан), синхронизация с 1С не используется.
Подробности и сценарий показа — docs/DEMO.md."""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(ROOT, "instance")
DEMO_DB = os.path.join(INSTANCE_DIR, "demo.db")


def _parse_args():
    parser = argparse.ArgumentParser(description="Демо-версия WMS для презентации")
    parser.add_argument("--reset", action="store_true", help="стереть демо-базу и наполнить заново")
    parser.add_argument("--lan", action="store_true", help="слушать все интерфейсы (для показа с телефона)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WMS_PORT", "5050")))
    parser.add_argument("--seed-only", action="store_true", help="только подготовить базу и выйти")
    return parser.parse_args()


def main():
    args = _parse_args()

    os.makedirs(INSTANCE_DIR, exist_ok=True)
    if args.reset and os.path.exists(DEMO_DB):
        os.remove(DEMO_DB)

    os.environ["WMS_DEMO"] = "1"
    os.environ["WMS_DATABASE_URL"] = "sqlite:///" + DEMO_DB
    os.environ["WMS_GOOGLE_CREDENTIALS_FILE"] = os.path.join(INSTANCE_DIR, "demo-google-disabled.json")
    os.environ["WMS_PUBLIC_URL"] = f"http://localhost:{args.port}"

    sys.path.insert(0, ROOT)
    from wms import create_app
    from wms.demo_data import DEMO_ACCOUNTS, DEMO_PASSWORD, seed_demo

    app = create_app()
    with app.app_context():
        seeded = seed_demo(app)

    print("=" * 64)
    print("WMS — ДЕМО-ВЕРСИЯ (данные вымышленные, база instance/demo.db)")
    print(f"База {'наполнена заново' if seeded else 'уже была наполнена (используем её; --reset — сбросить)'}")
    print(f"Адрес:  http://localhost:{args.port}")
    print(f"Пароль для всех демо-пользователей: {DEMO_PASSWORD}")
    for username, description in DEMO_ACCOUNTS:
        print(f"  {username:<12} {description}")
    print("=" * 64)

    if args.seed_only:
        return

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    app.run(host=host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
