"""Демо-данные для презентации (см. run_demo.py и docs/DEMO.md).

Все названия, штрихкоды, количества и люди здесь вымышленные. Наполнение
запускается ТОЛЬКО в демо-режиме (WMS_DEMO=1, отдельная база) — иначе
seed_demo() отказывается работать, чтобы случайно не засорить боевую базу.

Короба, перемещения, отгрузки и приемки проводятся через те же маршруты, что
и в живой работе (тестовый клиент Flask), а не записываются в БД «в обход»:
так факт плана, «в пути», недовоз/излишек и остальные расчеты на экранах
получаются настоящими, а не нарисованными."""

import random
from datetime import date, datetime, time, timedelta

from flask import current_app

from .extensions import db
from .models import (
    Box,
    BoxItem,
    Cell,
    MovementDocument,
    MovementLine,
    Nomenclature,
    ProductCategory,
    ProductionRecord,
    ReceivingDocument,
    ReceivingLine,
    ShipmentPlan,
    ShipmentPlanCityDeadline,
    ShipmentPlanLine,
    Supplier,
    UnplacedStock,
    User,
    Warehouse,
    Zone,
)
from .utils.numbering import next_number

DEMO_PASSWORD = "demo"

MAIN_WAREHOUSE = "Склад 1"
SECOND_WAREHOUSE = "Склад 2"
WORKSHOP_WAREHOUSE = "Склад 3"

DEMO_PRODUCT_COUNT = 18
DEMO_CATEGORIES = [
    # (название, норма минут на 1 шт, порог штук в коробе)
    ("Категория 1", 12, 60),
    ("Категория 2", 30, 40),
    ("Категория 3", 55, 30),
]

OZON_CITIES = [
    ("Направление 1", 78), ("Направление 2", 49), ("Направление 3", 38),
    ("Направление 4", 38), ("Направление 5", 38), ("Направление 6", 30),
]
WB_CITIES = [
    ("Направление 7", 125), ("Направление 8", 60), ("Направление 9", 40),
    ("Направление 10", 45), ("Направление 11", 40),
]


def _ensure_demo_mode():
    if not current_app.config.get("DEMO_MODE"):
        raise RuntimeError(
            "Демо-данные можно загружать только в демо-режиме (WMS_DEMO=1) — "
            "иначе они смешаются с боевыми. Запускайте через run_demo.py."
        )


def _create_users(main_wh, second_wh, workshop_wh):
    admin = User.query.filter_by(username="admin").first()
    if admin is None:
        admin = User(username="admin", is_admin=True)
        db.session.add(admin)
    admin.full_name = "Администратор (демо)"
    admin.is_admin = True
    admin.set_password(DEMO_PASSWORD)

    def _user(username, full_name, role="warehouse", **extra):
        user = User.query.filter_by(username=username).first()
        if user is None:
            user = User(username=username)
            db.session.add(user)
        user.full_name = full_name
        user.role = role
        user.set_password(DEMO_PASSWORD)
        for key, value in extra.items():
            setattr(user, key, value)
        return user

    storekeeper = _user(
        "kladovshik", "Кладовщик 1",
        warehouse_id=main_wh.id,
        movement_complete_allowed=True,
        movement_receive_allowed=True,
        movement_view_allowed=True,
        invoice_receiving_view_allowed=True,
    )
    logist = _user("logist", "Логист 1", role="logist")
    fulfillment = _user("fulfilment", "Фулфилмент (Склад 3)", role="fulfillment", warehouse_id=workshop_wh.id)
    fulfillment.allowed_sections = "receiving,movement"
    workers = [
        _user("shveya1", "Сотрудник 1", role="production"),
        _user("shveya2", "Сотрудник 2", role="production"),
        _user("shveya3", "Сотрудник 3", role="production"),
    ]
    db.session.commit()
    return admin, storekeeper, logist, fulfillment, workers


def _create_categories():
    # Демо-база отдельная и еще пустая: заменяем системные
    # примеры на обезличенные категории.
    ProductCategory.query.delete()
    for name, norm, warning in DEMO_CATEGORIES:
        db.session.add(
            ProductCategory(
                name=name,
                keywords="",
                norm_minutes=norm,
                box_qty_warning=warning,
            )
        )
    db.session.commit()


def _create_warehouses():
    main = Warehouse(code=next_number("warehouse"), name=MAIN_WAREHOUSE, address="")
    second = Warehouse(code=next_number("warehouse"), name=SECOND_WAREHOUSE, address="")
    workshop = Warehouse(code=next_number("warehouse"), name=WORKSHOP_WAREHOUSE, address="")
    db.session.add_all([main, second, workshop])
    db.session.commit()

    cells_by_warehouse = {}
    for warehouse, rows in ((main, ("A", "B", "C")), (second, ("A", "B")), (workshop, ("A",))):
        cells = []
        for row in rows:
            zone = Zone(warehouse_id=warehouse.id, code=f"Ряд {row}", name=f"Стеллаж {row}")
            db.session.add(zone)
            db.session.flush()
            for number in range(1, 9):
                cell = Cell(warehouse_id=warehouse.id, zone_id=zone.id, code=f"{row}{number:04d}")
                db.session.add(cell)
                cells.append(cell)
        cells_by_warehouse[warehouse.id] = cells
    db.session.commit()
    return main, second, workshop, cells_by_warehouse


def _create_nomenclature():
    items = []
    categories = ProductCategory.query.order_by(ProductCategory.id).all()
    for index in range(1, DEMO_PRODUCT_COUNT + 1):
        category = categories[(index - 1) % len(categories)]
        item = Nomenclature(
            sku=f"ART-{index:03d}",
            barcode=f"46000{index:08d}",
            name=f"Товар {index}",
            size=f"Размер {((index - 1) % 4) + 1}",
            unit="шт",
            category_id=category.id,
        )
        db.session.add(item)
        items.append(item)
    db.session.commit()
    # Один товар с доп. штрихкодом — чтобы на презентации показать, что план
    # и сканер находят его по любому из двух кодов.
    items[0].barcode2 = "46999000000001"
    db.session.commit()
    return items


def _new_box(rng, warehouse, cell, contents, days_ago=0):
    box = Box(
        box_number=next_number("box"),
        warehouse_id=warehouse.id,
        cell_id=cell.id if cell else None,
        status="stored" if cell else "open",
        created_at=datetime.utcnow() - timedelta(days=days_ago),
        warehouse_arrived_at=datetime.utcnow() - timedelta(days=days_ago),
    )
    db.session.add(box)
    db.session.flush()
    for item, qty in contents:
        db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
    return box


def _create_stock(rng, items, main, second, workshop, cells_by_warehouse):
    for warehouse, share in ((main, 1.0), (second, 0.55), (workshop, 0.3)):
        cells = cells_by_warehouse[warehouse.id]
        cell_cursor = 0
        for item in items:
            if rng.random() > share:
                continue
            for _ in range(rng.randint(1, 3)):
                qty = rng.choice([8, 10, 12, 15, 20, 24, 30, 36, 40])
                cell = cells[cell_cursor % len(cells)]
                cell_cursor += 1
                _new_box(rng, warehouse, cell, [(item, qty)], days_ago=rng.randint(1, 20))
    # Принятое, но еще не упакованное в короба («на разбраковке»).
    for item in items[:5]:
        UnplacedStock.add(main.id, item.id, rng.choice([30, 45, 60]))
    db.session.commit()


def _create_receivings(rng, items, main, admin):
    supplier = Supplier(name="Поставщик 1", inn="0000000000", phone="")
    db.session.add(supplier)
    db.session.commit()

    def _doc(status, order_number, lines, **extra):
        doc = ReceivingDocument(
            number=next_number("receiving"),
            warehouse_id=main.id,
            supplier=supplier.name,
            supplier_id=supplier.id,
            status=status,
            order_number=order_number,
            created_by_id=admin.id,
            **extra,
        )
        db.session.add(doc)
        db.session.flush()
        for item, qty, expected in lines:
            db.session.add(
                ReceivingLine(
                    document_id=doc.id, nomenclature_id=item.id, qty=qty,
                    expected_qty=expected, confirmed=expected is not None,
                )
            )
        return doc

    now = datetime.utcnow()
    _doc(
        "completed", "ЗП-0101",
        [(items[5], 120, 120), (items[6], 80, 80)],
        created_at=now - timedelta(days=9), completed_at=now - timedelta(days=9),
    )
    _doc(
        "sorting", "ЗП-0102",
        [(items[7], 90, 100), (items[8], 60, 60)],
        created_at=now - timedelta(days=1), sorting_started_at=now - timedelta(hours=3),
        invoice_file_name="Накладная_демо_0102.xlsx",
    )
    _doc("draft", "ЗП-0103", [(items[9], 40, None)], created_at=now)
    db.session.commit()


def _logged_in_client(app, admin):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin.id)
        sess["_fresh"] = True
        sess["session_version"] = admin.session_version or 0
    return client


def _create_plans(rng, items, admin, today):
    from .blueprints.shipment_plan import (
        MARKETPLACE_LABELS,
        _apply_priority_distribution,
        _get_or_create_city_warehouse,
    )

    period_start = today - timedelta(days=8)
    city_warehouses = {}
    for marketplace, cities in (("ozon", OZON_CITIES), ("wb", WB_CITIES)):
        plan = ShipmentPlan(
            marketplace=marketplace,
            sheet_name=f"Распределение {MARKETPLACE_LABELS[marketplace]} ФБС от {period_start.strftime('%d.%m')}",
            uploaded_by_id=admin.id,
            period_start=period_start,
        )
        db.session.add(plan)
        db.session.flush()
        for city, weight in cities:
            warehouse = _get_or_create_city_warehouse(marketplace, city)
            city_warehouses[(marketplace, city)] = warehouse
            for position, item in enumerate(items):
                base = 0.42 if position % 3 == 0 else 0.17
                planned = round(weight * base * rng.uniform(0.5, 1.2))
                db.session.add(
                    ShipmentPlanLine(
                        plan_id=plan.id,
                        warehouse_id=warehouse.id,
                        nomenclature_id=item.id,
                        barcode=item.barcode,
                        article=item.sku.rsplit("-", 1)[0],
                        size=item.size,
                        planned_qty=planned,
                        fulfilled_qty=0,
                        period_start=period_start,
                        # Приоритет 0/1/2 — несколько «горящих» позиций.
                        priority=0 if position % 7 == 0 else 1 if position % 5 == 0 else None,
                    )
                )
    db.session.flush()
    _apply_priority_distribution()
    db.session.commit()
    return city_warehouses, period_start


def _ship(client, rng, items, sender, city_wh, contents, days_ago, receive=None, marketplace="ozon"):
    """Собирает перемещение на город и проводит через те же маршруты, что и в
    живой работе: завершить сборку → заявка на маркетплейс → отгрузка
    транспортом → (опционально) приемка. contents — список коробов, каждый —
    список (товар, количество). receive: None — осталось «в пути»,
    "full" — принято полностью, "short" — принято с недовозом."""
    doc = MovementDocument(
        number=next_number("movement"),
        from_warehouse_id=sender.id,
        to_warehouse_id=city_wh.id,
        created_at=datetime.utcnow() - timedelta(days=days_ago + 1),
    )
    db.session.add(doc)
    db.session.flush()
    for box_contents in contents:
        box = _new_box(rng, sender, None, box_contents)
        db.session.flush()
        db.session.add(
            MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id)
        )
    doc.marketplace_request_number = f"{'OZ' if marketplace == 'ozon' else 'WB'}-{rng.randint(10000000, 99999999)}"
    db.session.commit()

    client.post(f"/movement/{doc.id}/complete")
    client.post(f"/movement/{doc.id}/mark-marketplace-request")
    client.post(f"/movement/{doc.id}/mark-shipped")

    doc = MovementDocument.query.get(doc.id)
    shipped_day = datetime.combine(date.today() - timedelta(days=days_ago), time(14, 0))
    doc.shipped_at = shipped_day
    doc.created_at = shipped_day - timedelta(hours=20)
    db.session.commit()

    if receive:
        form = {}
        for box_contents in contents:
            for item, qty in box_contents:
                form[f"qty_{item.id}"] = form.get(f"qty_{item.id}", 0) + qty
        if receive == "short":
            first_key = next(iter(form))
            form[first_key] = max(form[first_key] - 4, 0)
        client.post(f"/movement/{doc.id}/receive", data={k: str(v) for k, v in form.items()})
    return doc


def _create_movements(app, rng, items, main, second, workshop, city_warehouses, admin):
    client = _logged_in_client(app, admin)
    ozon = lambda city: city_warehouses[("ozon", city)]  # noqa: E731
    wb = lambda city: city_warehouses[("wb", city)]  # noqa: E731

    def pick(count, qty_range=(12, 40)):
        return [(item, rng.randint(*qty_range)) for item in rng.sample(items, count)]

    scenarios = [
        # (отправитель, город, коробов, дней назад, приемка, площадка)
        (main, ozon("Направление 1"), 3, 6, "full", "ozon"),
        (main, ozon("Направление 1"), 2, 2, None, "ozon"),
        (second, ozon("Направление 2"), 2, 5, "short", "ozon"),
        (main, ozon("Направление 3"), 3, 4, "full", "ozon"),
        (main, ozon("Направление 4"), 2, 3, None, "ozon"),
        (second, ozon("Направление 5"), 2, 1, None, "ozon"),
        (main, ozon("Направление 6"), 1, 7, "full", "ozon"),
        (main, wb("Направление 7"), 4, 5, "full", "wb"),
        (second, wb("Направление 7"), 3, 2, None, "wb"),
        (main, wb("Направление 8"), 3, 4, "short", "wb"),
        (main, wb("Направление 9"), 2, 3, "full", "wb"),
        (second, wb("Направление 10"), 2, 1, None, "wb"),
        (main, wb("Направление 11"), 2, 6, "full", "wb"),
    ]
    for sender, city_wh, boxes, days_ago, receive, marketplace in scenarios:
        contents = [pick(rng.randint(1, 2)) for _ in range(boxes)]
        _ship(client, rng, items, sender, city_wh, contents, days_ago, receive, marketplace)

    # Незавершенный черновик — виден в списке перемещений как «в работе».
    draft = MovementDocument(
        number=next_number("movement"), from_warehouse_id=main.id, to_warehouse_id=ozon("Направление 4").id,
        created_by_id=admin.id,
    )
    db.session.add(draft)
    db.session.flush()
    box = _new_box(rng, main, None, pick(1))
    db.session.flush()
    db.session.add(MovementLine(document_id=draft.id, box_id=box.id, from_warehouse_id=main.id))
    db.session.commit()


def _create_deadlines(city_warehouses, today):
    for marketplace in ("ozon", "wb"):
        plan = ShipmentPlan.query.filter_by(marketplace=marketplace).first()
        cities = OZON_CITIES if marketplace == "ozon" else WB_CITIES
        # Разные даты, в т.ч. уже прошедшая — чтобы на экране были и
        # выполненные к дате, и просроченные, и будущие города, а список
        # сортировался по дате.
        offsets = [-1, 1, 2, 4, 6, 9]
        for position, (city, _weight) in enumerate(cities):
            warehouse = city_warehouses[(marketplace, city)]
            db.session.add(
                ShipmentPlanCityDeadline(
                    plan_id=plan.id,
                    warehouse_id=warehouse.id,
                    ship_by_date=today + timedelta(days=offsets[position % len(offsets)]),
                )
            )
    db.session.commit()


def _create_production(rng, items, workers, today):
    primary_items = items[:5]
    for day_offset in range(5, -1, -1):
        work_date = today - timedelta(days=day_offset)
        if work_date.weekday() >= 5:
            continue
        for worker in workers:
            for index in range(rng.randint(20, 30)):
                item = rng.choice(primary_items if rng.random() < 0.7 else items)
                moment = datetime.combine(work_date, time(8, 30)) + timedelta(minutes=9 * index + rng.randint(0, 6))
                db.session.add(
                    ProductionRecord(
                        user_id=worker.id, nomenclature_id=item.id,
                        created_at=moment, work_date=work_date,
                    )
                )
    db.session.commit()


def seed_demo(app=None, seed=42):
    """Наполняет пустую демо-базу. Повторный вызов на уже наполненной базе
    ничего не делает (возвращает False) — чтобы презентацию нельзя было
    случайно испортить дублями."""
    _ensure_demo_mode()
    app = app or current_app._get_current_object()
    if Nomenclature.query.count() > 0:
        return False

    rng = random.Random(seed)
    today = date.today()

    _create_categories()
    main, second, workshop, cells_by_warehouse = _create_warehouses()
    admin, _storekeeper, _logist, _fulfillment, workers = _create_users(main, second, workshop)
    items = _create_nomenclature()
    _create_stock(rng, items, main, second, workshop, cells_by_warehouse)
    _create_receivings(rng, items, main, admin)
    city_warehouses, _period_start = _create_plans(rng, items, admin, today)
    _create_movements(app, rng, items, main, second, workshop, city_warehouses, admin)
    _create_deadlines(city_warehouses, today)
    _create_production(rng, items, workers, today)
    return True


DEMO_ACCOUNTS = [
    ("admin", "Администратор — полный доступ ко всем разделам"),
    ("kladovshik", "Кладовщик — приемка, размещение, перемещения"),
    ("logist", "Логист — передача отгрузок транспорту"),
    ("shveya1", "Производство — сканирование изделий (также shveya2, shveya3)"),
    ("fulfilment", "Фулфилмент — только свои приемки и перемещения"),
]
