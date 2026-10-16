"""Цифры для кейса «что дала WMS» — считаются по РЕАЛЬНОЙ базе склада.

Запуск на сервере (только чтение, ничего не меняет):
    cd /opt/wms && .venv/bin/python3 scripts/case_metrics.py            # 30 дней
    cd /opt/wms && .venv/bin/python3 scripts/case_metrics.py --days 60

Печатает объёмы, скорость сборки и отгрузки, качество приёмки на складах
назначения (недовоз/излишек) и выполнение плана отгрузок. Личные данные не
выводятся — только количества. Результат вставляется в кейс
(docs/commercial/01-case-study.md) рядом с цифрами «до WMS», которые вы
знаете сами (сколько времени и ошибок было на таблицах)."""

import argparse
import statistics
import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from wms import create_app
from wms.models import (
    Box,
    MovementDocument,
    ProductionRecord,
    ReceivingDocument,
    ShipmentPlan,
    User,
)


def _hours(delta):
    return delta.total_seconds() / 3600


def _fmt_hours(value):
    if value is None:
        return "—"
    return f"{value:.1f} ч" if value < 48 else f"{value / 24:.1f} дн"


def _pct(part, whole):
    return f"{100 * part / whole:.1f}%" if whole else "—"


def main(days):
    app = create_app()
    with app.app_context():
        since = datetime.utcnow() - timedelta(days=days)
        print(f"Период: последние {days} дн. (с {since:%d.%m.%Y})\n")

        # --- Объёмы ---------------------------------------------------
        receivings = ReceivingDocument.query.filter(
            ReceivingDocument.status == "completed", ReceivingDocument.completed_at >= since
        ).all()
        boxes_created = Box.query.filter(Box.created_at >= since).count()
        shipped_docs = MovementDocument.query.filter(
            MovementDocument.status == "completed", MovementDocument.shipped_at >= since
        ).all()
        shipped_qty = sum(doc.total_sent_qty() for doc in shipped_docs)
        shipped_boxes = sum(doc.lines.count() for doc in shipped_docs)
        authors = {doc.created_by_id for doc in shipped_docs if doc.created_by_id}
        print("== Объёмы ==")
        print(f"Завершённых приёмок:        {len(receivings)}")
        print(f"Принято единиц товара:      {sum(d.total_qty() for d in receivings):.0f}")
        print(f"Создано коробов:            {boxes_created}")
        print(f"Отгружено перемещений:      {len(shipped_docs)}")
        print(f"Отгружено коробов:          {shipped_boxes}")
        print(f"Отгружено единиц товара:    {shipped_qty:.0f}")
        print(f"Сотрудников собирали отгрузки: {len(authors)}")
        print(f"Отгрузок в день (в среднем): {len(shipped_docs) / days:.1f}")

        # --- Скорость -------------------------------------------------
        durations = [
            _hours(doc.shipped_at - doc.created_at)
            for doc in shipped_docs
            if doc.created_at and doc.shipped_at and doc.shipped_at >= doc.created_at
        ]
        print("\n== Скорость: от создания перемещения до отгрузки транспортом ==")
        if durations:
            print(f"Среднее:  {_fmt_hours(statistics.mean(durations))}")
            print(f"Медиана:  {_fmt_hours(statistics.median(durations))}")
        else:
            print("Нет данных")

        # --- Качество приёмки на МП -----------------------------------
        received = [doc for doc in shipped_docs if doc.received_at]
        with_discrepancy = [doc for doc in received if list(doc.discrepancies)]
        shortage = excess = 0.0
        for doc in received:
            for row in doc.discrepancies:
                shortage += row.shortage_qty()
                excess += row.excess_qty()
        sent_for_received = sum(doc.total_sent_qty() for doc in received)
        print("\n== Приёмка на складах назначения (расхождения) ==")
        print(f"Принято перемещений:        {len(received)} из {len(shipped_docs)}")
        print(f"С расхождением:             {len(with_discrepancy)} ({_pct(len(with_discrepancy), len(received))})")
        print(f"Недовоз, единиц:            {shortage:.0f} ({_pct(shortage, sent_for_received)} от отправленного)")
        print(f"Излишек, единиц:            {excess:.0f} ({_pct(excess, sent_for_received)} от отправленного)")

        # --- План отгрузок --------------------------------------------
        print("\n== План отгрузок (как на дашборде) ==")
        try:
            from wms.blueprints.shipment_plan import _dashboard_context

            for mp in _dashboard_context()["marketplaces"]:
                if not mp.get("plan"):
                    print(f"{mp['label']}: план не загружен")
                    continue
                planned, shipped = mp["total_planned"], mp["total_in_transit"]
                print(
                    f"{mp['label']}: план {planned:.0f}, отгружено {shipped:.0f} "
                    f"({_pct(shipped, planned)})"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"Не удалось посчитать: {exc}")

        # --- Производство ---------------------------------------------
        scans = ProductionRecord.query.filter(ProductionRecord.created_at >= since).all()
        if scans:
            print("\n== Производство ==")
            print(f"Отсканировано изделий:      {len(scans)}")
            print(f"Сотрудников со сканами:     {len({r.user_id for r in scans})}")

        print(f"\nВсего пользователей в системе: {User.query.count()}")
        print(
            "\nДальше: впишите цифры «до WMS» (время сборки, число ошибок/пересортов "
            "в месяц) в docs/commercial/01-case-study.md и сравните."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Цифры для кейса по реальной базе (только чтение)")
    parser.add_argument("--days", type=int, default=30)
    main(parser.parse_args().days)
