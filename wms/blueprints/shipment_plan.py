from datetime import date, timedelta

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from ..extensions import db
from ..models import (
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    Nomenclature,
    ProductionRecord,
    ReceivingDocument,
    ReceivingLine,
    ShipmentPlan,
    ShipmentPlanLine,
    UnplacedStock,
    Warehouse,
)
from ..utils.excel_io import export_shipment_plan_to_excel, timestamp_for_filename
from ..utils.http import content_disposition
from ..utils.numbering import next_number
from ..utils.shipment_plan_import import extract_period_start, parse_plan_sheet

bp = Blueprint("shipment_plan", __name__)

MARKETPLACES = ("ozon", "wb")
MARKETPLACE_LABELS = {"ozon": "ОЗОН", "wb": "ВБ"}
PERIOD_DAYS = 14


def _get_or_create_city_warehouse(marketplace, city_name):
    wh = Warehouse.query.filter_by(marketplace=marketplace, marketplace_city=city_name).first()
    if wh:
        return wh
    wh = Warehouse(
        code=next_number("warehouse"),
        name=f"{MARKETPLACE_LABELS[marketplace]}: {city_name}",
        marketplace=marketplace,
        marketplace_city=city_name,
    )
    db.session.add(wh)
    db.session.flush()
    return wh


def _apply_plan(marketplace, parsed):
    """Полностью заменяет строки плана этого маркетплейса новыми из файла."""
    plan = ShipmentPlan.query.filter_by(marketplace=marketplace).first()
    if not plan:
        plan = ShipmentPlan(marketplace=marketplace)
        db.session.add(plan)

    plan.sheet_name = parsed.sheet_name
    plan.uploaded_by_id = current_user.id
    plan.period_start = extract_period_start(parsed.sheet_name)

    plan.lines.delete()

    city_warehouses = {
        city: _get_or_create_city_warehouse(marketplace, city) for city in parsed.cities
    }

    barcodes = {row["barcode"] for row in parsed.rows}
    nomenclature_by_barcode = {
        n.barcode: n
        for n in Nomenclature.query.filter(Nomenclature.barcode.in_(barcodes)).all()
    }

    # Один и тот же штрихкод изредка встречается в файле больше одного раза
    # для одного и того же города (дубль строки при ручном ведении таблицы) —
    # схлопываем такие дубли суммированием количества, а не падаем на
    # уникальном ограничении (plan, склад, штрихкод).
    merged = {}
    for row in parsed.rows:
        key = (row["city"], row["barcode"])
        if key in merged:
            merged[key]["qty"] += row["qty"]
            merged[key]["fact"] += row["fact"]
        else:
            merged[key] = dict(row)

    created = 0
    unmatched_barcodes = set()
    for row in merged.values():
        nomenclature = nomenclature_by_barcode.get(row["barcode"])
        if nomenclature is None:
            unmatched_barcodes.add(row["barcode"])
        db.session.add(
            ShipmentPlanLine(
                plan=plan,
                warehouse_id=city_warehouses[row["city"]].id,
                nomenclature_id=nomenclature.id if nomenclature else None,
                barcode=row["barcode"],
                article=row["article"],
                size=row["size"],
                planned_qty=row["qty"],
                # Факт "отгружено / в пути" из самого файла плана — уже
                # известное на момент выгрузки выполнение, а не только то,
                # что WMS увидит через будущие перемещения.
                fulfilled_qty=row.get("fact", 0.0),
            )
        )
        created += 1

    return created, len(unmatched_barcodes)


@bp.route("/upload", methods=["GET", "POST"])
def upload():
    if not current_user.is_admin:
        flash("Загружать план отгрузок может только администратор", "danger")
        return redirect(url_for("shipment_plan.dashboard"))

    if request.method == "GET":
        return render_template("shipment_plan/upload.html")

    file = request.files.get("file")
    if not file or file.filename == "":
        flash("Выберите файл xlsx", "danger")
        return redirect(url_for("shipment_plan.upload"))

    data = file.read()
    summary = []
    found_any = False
    for marketplace in MARKETPLACES:
        import io as _io

        parsed = parse_plan_sheet(_io.BytesIO(data), marketplace)
        if parsed is None:
            continue
        found_any = True
        created, unmatched = _apply_plan(marketplace, parsed)
        summary.append(
            f"{MARKETPLACE_LABELS[marketplace]} («{parsed.sheet_name}»): "
            f"{created} позиций, городов {len(parsed.cities)}, "
            f"неизвестных штрихкодов {unmatched}"
        )

    if not found_any:
        flash(
            "В файле не найден ни один лист «Распределение ОЗОН ФБС ...» "
            "или «Распределение ВБ ФБС ...»",
            "danger",
        )
        return redirect(url_for("shipment_plan.upload"))

    db.session.commit()
    flash("План отгрузок обновлен: " + "; ".join(summary), "success")
    return redirect(url_for("shipment_plan.dashboard"))


def _sender_warehouse_ids():
    """Склады-отправители — все обычные (не городские склады маркетплейсов)."""
    return [
        wh.id
        for wh in Warehouse.query.filter_by(marketplace=None, is_active=True).all()
    ]


def _stock_by_nomenclature(warehouse_ids):
    """{nomenclature_id: суммарный остаток} по заданным складам — неразмещенный
    остаток плюс товар, упакованный в короба на этих складах (независимо от
    того, размещен ли короб в ячейке)."""
    if not warehouse_ids:
        return {}

    stock = {}
    for nomenclature_id, qty in (
        db.session.query(UnplacedStock.nomenclature_id, func.sum(UnplacedStock.qty))
        .filter(UnplacedStock.warehouse_id.in_(warehouse_ids))
        .group_by(UnplacedStock.nomenclature_id)
        .all()
    ):
        stock[nomenclature_id] = stock.get(nomenclature_id, 0) + (qty or 0)

    for nomenclature_id, qty in (
        db.session.query(BoxItem.nomenclature_id, func.sum(BoxItem.qty))
        .join(Box, BoxItem.box_id == Box.id)
        .filter(Box.warehouse_id.in_(warehouse_ids))
        .group_by(BoxItem.nomenclature_id)
        .all()
    ):
        stock[nomenclature_id] = stock.get(nomenclature_id, 0) + (qty or 0)

    return stock


def _unplaced_by_nomenclature(warehouse_ids):
    """{nomenclature_id: кол-во} товара, который принят, но еще не упакован
    в короб (висит в UnplacedStock) — по сути, "на разбраковке": уже на
    складе, но пока не готов к отгрузке. Отдельно от _stock_by_nomenclature,
    которая считает вообще любой остаток (включая уже упакованный)."""
    if not warehouse_ids:
        return {}
    return {
        nomenclature_id: qty or 0
        for nomenclature_id, qty in (
            db.session.query(UnplacedStock.nomenclature_id, func.sum(UnplacedStock.qty))
            .filter(UnplacedStock.warehouse_id.in_(warehouse_ids))
            .group_by(UnplacedStock.nomenclature_id)
            .all()
        )
    }


def _pending_sorting_by_nomenclature(warehouse_ids):
    """{nomenclature_id: кол-во} по строкам приемок в статусе "На пересчете"
    или "На разбраковке" (см. ReceivingDocument.status) — с этого момента
    (кнопка "Отправить на пересчет") товар физически уже принят на складе,
    но еще не попадает в UnplacedStock (это происходит только при
    завершении приемки, см. receiving.complete) — без этой раскладки он
    "исчезал" бы из плана отгрузок на все время пересчета/разбраковки,
    как будто его еще нет на складе. Годное кол-во считаем за вычетом уже
    выделенного брака (defect_qty) — бракованное в остаток не попадет.
    Упакованные в короб прямо при приемке строки (box_id заполнен) сюда не
    входят — они разбраковке не подлежат и не были неразмещенными."""
    if not warehouse_ids:
        return {}
    rows = (
        db.session.query(
            ReceivingLine.nomenclature_id,
            func.sum(ReceivingLine.qty - ReceivingLine.defect_qty),
        )
        .join(ReceivingDocument, ReceivingDocument.id == ReceivingLine.document_id)
        .filter(
            ReceivingDocument.warehouse_id.in_(warehouse_ids),
            ReceivingDocument.status.in_(("recounting", "sorting")),
            ReceivingLine.box_id.is_(None),
        )
        .group_by(ReceivingLine.nomenclature_id)
        .all()
    )
    return {nomenclature_id: qty or 0 for nomenclature_id, qty in rows}


def _production_by_nomenclature(nomenclature_ids, period_start):
    """{nomenclature_id: кол-во} отсканированного на производстве с начала
    периода плана — просто сумма, без вычета уже принятого через Приемку
    (прямой связи между сканом на производстве и конкретной строкой
    приемки в системе нет, а точный remainder было бы сложно и не нужно
    считать — здесь просто информационная цифра "сколько сделано за
    период", а не точный остаток на цеху)."""
    if not nomenclature_ids or not period_start:
        return {}
    rows = (
        db.session.query(ProductionRecord.nomenclature_id, func.count(ProductionRecord.id))
        .filter(
            ProductionRecord.nomenclature_id.in_(nomenclature_ids),
            ProductionRecord.work_date >= period_start,
        )
        .group_by(ProductionRecord.nomenclature_id)
        .all()
    )
    return {nomenclature_id: qty for nomenclature_id, qty in rows}


def _in_transit_by_warehouse_and_item():
    """{(warehouse_id, nomenclature_id): кол-во} товара, уже отправленного
    перемещением на этот склад-город (документ завершен), но еще не
    подтвержденного кнопкой "Принято на складе" — висит как "в пути".
    fulfilled_qty у строки плана дописывается только при приемке (см.
    movement.receive), поэтому без этой раскладки по товарам "Что нужно
    отправить" ниже продолжал бы требовать полное количество по плану, как
    будто ничего еще не выехало — раньше это было видно только в сводке по
    городу целиком, а не по конкретной позиции."""
    rows = (
        db.session.query(
            MovementDocument.to_warehouse_id,
            BoxItem.nomenclature_id,
            func.sum(BoxItem.qty),
        )
        .join(MovementLine, MovementLine.document_id == MovementDocument.id)
        .join(BoxItem, BoxItem.box_id == MovementLine.box_id)
        .filter(MovementDocument.status == "completed", MovementDocument.received_at.is_(None))
        .group_by(MovementDocument.to_warehouse_id, BoxItem.nomenclature_id)
        .all()
    )
    return {(wh_id, nom_id): qty or 0 for wh_id, nom_id, qty in rows}


def _pace_analysis(plan, total_planned, total_fulfilled):
    """Успеваем ли отгрузить план за 14 дней с даты из названия листа, и
    сколько дней потребуется при сегодняшнем темпе. Темп считается как
    среднее "выполнено / дней с начала периода" — то есть за весь период,
    включая факт, уже отгруженный на момент выгрузки файла (см.
    ShipmentPlanLine.fulfilled_qty), а не только то, что прошло через WMS."""
    if not plan.period_start:
        return None

    today = date.today()
    days_elapsed = (today - plan.period_start).days
    deadline = plan.period_start + timedelta(days=PERIOD_DAYS)
    days_left = (deadline - today).days
    remaining = max(total_planned - total_fulfilled, 0)

    rate = total_fulfilled / days_elapsed if days_elapsed > 0 else None
    days_needed = remaining / rate if rate and rate > 0 else None
    required_rate = remaining / days_left if days_left > 0 else None

    if remaining <= 0:
        status = "done"
    elif days_left <= 0:
        status = "overdue"
    elif rate is None or rate <= 0:
        status = "no_data"
    elif days_needed <= days_left:
        status = "on_track"
    else:
        status = "behind"

    return {
        "deadline": deadline,
        "days_elapsed": days_elapsed,
        "days_left": days_left,
        "rate": rate,
        "required_rate": required_rate,
        "days_needed": days_needed,
        "remaining": remaining,
        "status": status,
    }


@bp.route("/")
def dashboard():
    plans = {p.marketplace: p for p in ShipmentPlan.query.all()}
    sender_ids = _sender_warehouse_ids()
    stock = _stock_by_nomenclature(sender_ids)
    unplaced_stock = _unplaced_by_nomenclature(sender_ids)
    for nomenclature_id, qty in _pending_sorting_by_nomenclature(sender_ids).items():
        stock[nomenclature_id] = stock.get(nomenclature_id, 0) + qty
        unplaced_stock[nomenclature_id] = unplaced_stock.get(nomenclature_id, 0) + qty
    in_transit_by_item = _in_transit_by_warehouse_and_item()
    in_transit_by_warehouse = {}
    for (wh_id, _nom_id), qty in in_transit_by_item.items():
        in_transit_by_warehouse[wh_id] = in_transit_by_warehouse.get(wh_id, 0) + qty

    marketplaces_data = []
    lines_by_marketplace = {}
    for marketplace in MARKETPLACES:
        plan = plans.get(marketplace)
        if not plan:
            marketplaces_data.append(
                {"marketplace": marketplace, "label": MARKETPLACE_LABELS[marketplace], "plan": None}
            )
            continue

        lines = plan.lines.all()
        lines_by_marketplace[marketplace] = lines

        # Сколько по этой позиции уже едет (отправлено перемещением, но еще
        # не подтверждено кнопкой "Принято на складе") — показывается
        # отдельным числом рядом с потребностью, но саму потребность не
        # уменьшает: пока товар физически не проверен на месте, план по
        # нему остается открытым (тот же принцип, что и у fulfilled_qty,
        # которая тоже засчитывается только по факту приемки).
        for line in lines:
            line.in_transit_qty = in_transit_by_item.get((line.warehouse_id, line.nomenclature_id), 0)

        by_warehouse = {}
        for line in lines:
            row = by_warehouse.setdefault(
                line.warehouse_id,
                {"warehouse": line.warehouse, "planned": 0, "fulfilled": 0},
            )
            row["planned"] += line.planned_qty
            row["fulfilled"] += line.fulfilled_qty
        cities = sorted(by_warehouse.values(), key=lambda r: r["warehouse"].marketplace_city)
        for row in cities:
            row["in_transit"] = in_transit_by_warehouse.get(row["warehouse"].id, 0)

        # Штрихкоды с невыполненным остатком, для которых нечем отгружать —
        # только для значка-счетчика на карточке; сам список товаров теперь
        # общий для обоих маркетплейсов (см. picking_list ниже), поэтому
        # здесь достаточно посчитать количество, не строя весь список.
        problem_barcodes = {
            line.barcode
            for line in lines
            if line.remaining_qty() > 0
            and (line.nomenclature_id is None or stock.get(line.nomenclature_id, 0) <= 0)
        }

        total_planned = sum(line.planned_qty for line in lines)
        total_fulfilled = sum(line.fulfilled_qty for line in lines)
        total_in_transit = sum(row["in_transit"] for row in cities)
        pace = _pace_analysis(plan, total_planned, total_fulfilled)

        marketplaces_data.append(
            {
                "marketplace": marketplace,
                "label": MARKETPLACE_LABELS[marketplace],
                "plan": plan,
                "cities": cities,
                "problems_count": len(problem_barcodes),
                "total_planned": total_planned,
                "total_fulfilled": total_fulfilled,
                "total_in_transit": total_in_transit,
                # В шапке карточки "выполнено" теперь учитывает и то, что уже
                # едет (в пути) — по просьбе: общее выполнение плана должно
                # включать отправленное, а не только подтвержденное приемкой.
                # В табличной части ниже (по городам и по товарам) ничего не
                # меняем — там как было, "потребность (в пути)" отдельно.
                "total_fulfilled_with_transit": total_fulfilled + total_in_transit,
                "pace": pace,
            }
        )

    # Общий список товаров сразу по обоим маркетплейсам — артикул, размер,
    # штрихкод и наличие на складе-отправителе почти всегда одни и те же
    # для ОЗОН и ВБ (один и тот же товар торгуется на обеих площадках), а
    # раньше это все дублировалось в двух почти одинаковых таблицах. Теперь
    # одна строка на штрихкод, а города каждого маркетплейса — отдельными
    # блоками колонок (ОЗОН / ВБ) в той же строке.
    products = {}
    for marketplace, lines in lines_by_marketplace.items():
        for line in lines:
            product = products.setdefault(
                line.barcode,
                {
                    "barcode": line.barcode,
                    "article": line.article,
                    "size": line.size,
                    "no_stock": line.nomenclature_id is None
                    or stock.get(line.nomenclature_id, 0) <= 0,
                    # Принято, но еще не упаковано в короб ("на разбраковке") —
                    # отдельно от no_stock: товар физически есть на складе,
                    # просто еще не готов к отгрузке.
                    "unplaced": unplaced_stock.get(line.nomenclature_id, 0)
                    if line.nomenclature_id is not None
                    else 0,
                    # Готово к отгрузке — уже упаковано в короб (независимо от
                    # того, расставлен ли короб по ячейке), в отличие от
                    # "на разбраковке" выше. stock включает и то, и другое.
                    "ready_to_ship": max(
                        stock.get(line.nomenclature_id, 0)
                        - unplaced_stock.get(line.nomenclature_id, 0),
                        0,
                    )
                    if line.nomenclature_id is not None
                    else 0,
                    "ozon": {},
                    "wb": {},
                    "max_remaining": 0,
                    "in_transit_total": 0,
                },
            )
            product[marketplace][line.warehouse.marketplace_city] = line
            product["max_remaining"] = max(product["max_remaining"], line.remaining_qty())
            product["in_transit_total"] += line.in_transit_qty

    picking_list = sorted(
        (p for p in products.values() if p["max_remaining"] > 0),
        key=lambda p: (p["article"] or "", p["size"] or ""),
    )

    def _city_names(marketplace):
        for m in marketplaces_data:
            if m["marketplace"] == marketplace and m.get("cities"):
                return [row["warehouse"].marketplace_city for row in m["cities"]]
        return []

    ozon_cities = _city_names("ozon")
    wb_cities = _city_names("wb")

    # Итоговая строка над списком "Что нужно отправить" — просто сумма по
    # каждой колонке (На разбраковке/Готово к отгрузке/В пути и каждый
    # город), чтобы сразу видеть общий объем не пролистывая/не считая
    # вручную по строкам.
    picking_totals = {
        "unplaced": sum(p["unplaced"] for p in picking_list),
        "ready_to_ship": sum(p["ready_to_ship"] for p in picking_list),
        "in_transit": sum(p["in_transit_total"] for p in picking_list),
        "ozon": {
            city: sum(
                p["ozon"][city].remaining_qty() for p in picking_list if city in p["ozon"]
            )
            for city in ozon_cities
        },
        "wb": {
            city: sum(p["wb"][city].remaining_qty() for p in picking_list if city in p["wb"])
            for city in wb_cities
        },
    }

    # Сводка в шапке страницы: "в пути" по каждому маркетплейсу и общим
    # итогом, плюс "на складе" и "на производстве" — эти два уже не по
    # маркетплейсам (один и тот же остаток/цех работает на оба сразу).
    nomenclature_ids = {
        line.nomenclature_id
        for lines in lines_by_marketplace.values()
        for line in lines
        if line.nomenclature_id is not None
    }
    overall_stock = sum(stock.get(nid, 0) for nid in nomenclature_ids)
    period_starts = [p.period_start for p in plans.values() if p.period_start]
    earliest_period_start = min(period_starts) if period_starts else None
    production_by_nomenclature = _production_by_nomenclature(nomenclature_ids, earliest_period_start)
    overall_production = sum(production_by_nomenclature.values())
    overall_in_transit = sum(m.get("total_in_transit", 0) for m in marketplaces_data)

    summary = {
        "in_transit_by_marketplace": [
            {"label": m["label"], "qty": m.get("total_in_transit", 0)}
            for m in marketplaces_data
            if m.get("plan")
        ],
        "total_in_transit": overall_in_transit,
        "total_stock": overall_stock,
        "total_production": overall_production,
    }

    return render_template(
        "shipment_plan/dashboard.html",
        marketplaces=marketplaces_data,
        picking_list=picking_list,
        picking_totals=picking_totals,
        ozon_cities=ozon_cities,
        wb_cities=wb_cities,
        summary=summary,
    )


@bp.route("/export.xlsx")
def export_all():
    lines = ShipmentPlanLine.query.join(ShipmentPlan).all()
    data = export_shipment_plan_to_excel(lines)
    fname = f"shipment_plan_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )
