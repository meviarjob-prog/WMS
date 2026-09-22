"""Обмен с внешней обработкой 1С: выгрузка завершенных документов
перемещения и инвентаризации, которых 1С еще не забирала.

Аутентификация — отдельным токеном (не логином/паролем пользователя WMS),
потому что запрос идет не из браузера, а из 1С по HTTP. Токен сравнивается
с тем, что администратор сгенерировал на странице настроек этой
интеграции; эти два route (export/export_confirm) сознательно исключены
из общей проверки логина в wms/__init__.py — см. API_1C_PUBLIC_ENDPOINTS.
"""

import secrets
from datetime import datetime, time, timedelta

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from ..extensions import db
from ..models import (
    AppSetting, InventoryDocument, MovementDocument, OneCQuantityCheck,
    ReceivingDocument, SupplierReturn,
)

bp = Blueprint("integration_1c", __name__)

TOKEN_KEY = "api_1c_token"

# 1С не заводит отдельный физический склад под каждый склад-город
# маркетплейса (те создаются в WMS автоматически при загрузке плана
# отгрузок) — весь такой товар в 1С учитывается как уехавший на один общий
# промежуточный склад. Реальные оба склада указываются только когда товар
# едет между ними напрямую (внутреннее перемещение, не в сторону
# маркетплейса).
FULFILLMENT_WAREHOUSE_NAME = "Товары в пути на Фулфилмент"
DIRECT_TRANSFER_WAREHOUSE_NAMES = {"Основной склад", "Склад №2 (Шоссейная 167)"}


def _to_warehouse_name_for_1c(doc):
    """Каждый документ перемещения выгружается сам по себе, как и раньше —
    здесь меняется только то, ЧЬЕ имя склада подставляется в него в поле
    получателя, без какой-либо группировки/объединения документов между
    собой."""
    from_name = doc.from_warehouse.name if doc.from_warehouse else ""
    to_name = doc.to_warehouse.name if doc.to_warehouse else ""
    if (
        from_name in DIRECT_TRANSFER_WAREHOUSE_NAMES
        and to_name in DIRECT_TRANSFER_WAREHOUSE_NAMES
    ):
        return to_name
    # Склад-город маркетплейса (например "ОЗОН: Казань") — 1С ведет свой
    # набор промежуточных складов, настраиваемых отдельно на КАЖДЫЙ
    # склад-город (не на город целиком): одна и та же площадка одного
    # города может ехать на разные склады 1С в зависимости от маркетплейса
    # (например, ВБ Краснодар — на СЦ, а ОЗОН Краснодар — на фулфилмент),
    # см. Warehouse.fulfillment_1c_name и warehouses.update_fulfillment_1c_name
    # — настраивается администратором на странице «Настройки». Не настроено
    # для этого склада — используем общий запасной склад, как было раньше.
    if doc.to_warehouse and doc.to_warehouse.fulfillment_1c_name:
        return doc.to_warehouse.fulfillment_1c_name
    return FULFILLMENT_WAREHOUSE_NAME

# Эти endpoint'ы обмена с 1С не требуют логина в WMS — только токен
# (см. проверку в самих view). Импортируется в wms/__init__.py.
API_1C_PUBLIC_ENDPOINTS = {
    "integration_1c.export",
    "integration_1c.export_confirm",
    "integration_1c.reconciliation",
}


def _get_token():
    setting = AppSetting.query.get(TOKEN_KEY)
    return setting.value if setting else None


def _check_token():
    token = request.headers.get("X-1C-Token") or request.args.get("token")
    expected = _get_token()
    return bool(expected) and token == expected


def _pending_movements_query():
    return MovementDocument.query.filter_by(status="completed", synced_to_1c_at=None).filter(
        MovementDocument.marketplace_request_created_at.isnot(None),
        MovementDocument.marketplace_request_number.isnot(None),
        MovementDocument.marketplace_request_number != "",
        MovementDocument.accounting_entered_at.is_(None),
    )


def _pending_inventories_query():
    return InventoryDocument.query.filter_by(status="completed", synced_to_1c_at=None).filter(
        InventoryDocument.accounting_entered_at.is_(None)
    )


def _admin_required():
    """Общая проверка для страниц/действий очереди выгрузки — возвращает
    редирект, если доступ запрещен, иначе None (см. вызовы ниже)."""
    if not current_user.is_admin:
        flash("Доступно только администратору", "danger")
        return redirect(url_for("main.index"))
    return None


@bp.route("/", methods=["GET", "POST"])
def settings():
    if not current_user.is_admin:
        flash("Настройки интеграции доступны только администратору", "danger")
        return redirect(url_for("main.index"))

    if request.method == "POST":
        token = secrets.token_urlsafe(24)
        setting = AppSetting.query.get(TOKEN_KEY)
        if not setting:
            setting = AppSetting(key=TOKEN_KEY)
            db.session.add(setting)
        setting.value = token
        db.session.commit()
        flash("Новый токен сгенерирован — старый перестал действовать", "success")

    pending_movements = _pending_movements_query().count()
    pending_inventories = _pending_inventories_query().count()
    pending_supplier_returns = SupplierReturn.query.filter(
        SupplierReturn.synced_to_1c_at.is_(None),
        SupplierReturn.invoice_number.isnot(None),
        SupplierReturn.accounting_entered_at.is_(None),
    ).count()
    pending_receiving_adjustments = len(_receiving_adjustments_export())
    pending_movement_corrections = len(_movement_corrections_candidates())

    return render_template(
        "integration_1c/settings.html",
        token=_get_token(),
        pending_movements=pending_movements,
        pending_inventories=pending_inventories,
        pending_supplier_returns=pending_supplier_returns,
        pending_receiving_adjustments=pending_receiving_adjustments,
        pending_movement_corrections=pending_movement_corrections,
    )


def _movement_payload(doc):
    lines = []
    for line in doc.lines:
        for item in line.box.items:
            lines.append(
                {
                    "barcode": item.nomenclature.barcode,
                    "name": item.nomenclature.name,
                    "qty": item.qty,
                }
            )
    to_warehouse_name = doc.to_warehouse.name if doc.to_warehouse else ""
    comment = f"WMS: {doc.number} (склад получатель: {to_warehouse_name})"
    if doc.marketplace_request_number:
        # Номер заявки на приемку у маркетплейса (вносится вручную в списке
        # перемещений, см. movement.update_marketplace_request_number) —
        # рядом с номером перемещения (уже есть в начале комментария), чтобы
        # можно было найти документ в 1С по любому из двух номеров, без
        # повторного дублирования номера перемещения в конце строки.
        comment += f" (№ заявки МП: {doc.marketplace_request_number})"
    return {
        "id": doc.id,
        "number": doc.number,
        "date": (doc.completed_at or doc.created_at).isoformat(),
        "from_warehouse": doc.from_warehouse.name if doc.from_warehouse else "",
        "to_warehouse": _to_warehouse_name_for_1c(doc),
        # Реальный склад-город WMS (например, "ОЗОН: Казань") — когда
        # to_warehouse выше подменен на общий "Товары в пути на
        # Фулфилмент" (см. _to_warehouse_name_for_1c), это единственное
        # место, где виден настоящий адресат перемещения.
        "comment": comment,
        "lines": lines,
    }


def _inventory_payload(doc):
    return {
        "id": doc.id,
        "number": doc.number,
        "date": (doc.completed_at or doc.created_at).isoformat(),
        "warehouse": doc.warehouse.name if doc.warehouse else "",
        "comment": f"WMS: {doc.number}",
        "lines": [
            {
                "barcode": line.nomenclature.barcode,
                "name": line.nomenclature.name,
                "qty": line.qty,
            }
            for line in doc.lines
        ],
    }


def _aggregated_lines(lines):
    """Сводит одинаковые товары в одну строку для повторной сверки.

    В перемещении один SKU может лежать в нескольких коробах, а в 1С эти
    строки могут быть объединены. Поэтому ретроспективная сверка сравнивает
    итог по товару, а не порядок/количество строк табличной части.
    """
    grouped = {}
    for line in lines:
        barcode = str(line["barcode"] or "").strip()
        name = str(line["name"] or "").strip()
        key = barcode or name.casefold()
        if not key:
            continue
        row = grouped.setdefault(key, {"barcode": barcode, "name": name, "qty": 0})
        row["qty"] += float(line["qty"] or 0)
    return list(grouped.values())


def _movement_reconciliation_payload(doc):
    lines = _aggregated_lines(
        {
            "barcode": item.nomenclature.barcode,
            "name": item.nomenclature.name,
            "qty": item.qty,
        }
        for movement_line in doc.lines
        for item in movement_line.box.items
    )
    grouped = {(row["barcode"] or row["name"].casefold()): row for row in lines}
    # После «Принято на складе» недовоз физически убирается из коробов.
    # Документ перемещения в 1С, однако, отражает именно отправленное
    # количество. Восстанавливаем его построчно из зафиксированного
    # расхождения, иначе честный недовоз выглядел бы как ошибка обмена.
    for discrepancy in doc.discrepancies:
        item = discrepancy.nomenclature
        key = str(item.barcode or "").strip() or item.name.strip().casefold()
        grouped[key] = {
            "barcode": str(item.barcode or "").strip(),
            "name": item.name,
            "qty": float(discrepancy.expected_qty),
        }
    return {
        "id": doc.id,
        "number": doc.number,
        "lines": list(grouped.values()),
    }


def _receiving_reconciliation_payload(doc):
    return {
        "id": doc.id,
        "number": doc.number,
        "invoice_number": doc.number,
        "lines": _aggregated_lines(
            {
                "barcode": line.nomenclature.barcode,
                "name": line.nomenclature.name,
                "qty": line.qty,
            }
            for line in doc.lines
        ),
    }


def _first_order_number(order_number):
    """Если накладная закрывает сразу несколько заявок (см. чат — например
    «ВБ-К11/ВБ-К10» или «ВБ-К11, ВБ-К10»), в 1С возврат все равно можно
    привязать только к ОДНОМУ «Заказу поставщику» — берем первый из
    перечисленных, даже если конкретная возвращаемая позиция по факту
    относится к другой заявке из этого же списка. Разбирать возврат по
    заявкам построчно WMS сейчас не умеет — эта привязка сугубо
    приблизительная, для реального разделения нужно заводить накладную
    отдельными документами приемки (по одному на заявку)."""
    if not order_number:
        return order_number
    for sep in ("/", ","):
        if sep in order_number:
            return order_number.split(sep, 1)[0].strip()
    return order_number.strip()


def _supplier_returns_export():
    """Возвраты поставщику из разбраковки приемок (см. receiving.complete) —
    группируем по приемке в один документ на 1С с несколькими строками
    (одна разбраковка = один возврат), а не документ на каждую позицию.
    Возвраты без invoice_number (ручное списание из "Размещения", либо
    разбраковка приемки, не загруженной из файла накладной — см.
    ReceivingDocument.is_from_invoice_import) не выгружаем вовсе: 1С не с
    чем сопоставлять документ поступления, такие возвраты вносятся в 1С
    вручную."""
    returns = (
        SupplierReturn.query.filter(
            SupplierReturn.synced_to_1c_at.is_(None),
            SupplierReturn.invoice_number.isnot(None),
            SupplierReturn.accounting_entered_at.is_(None),
        )
        .order_by(SupplierReturn.receiving_document_id, SupplierReturn.id)
        .all()
    )

    groups = {}
    for ret in returns:
        groups.setdefault(ret.receiving_document_id, []).append(ret)

    payloads = []
    for receiving_document_id, group in groups.items():
        first = group[0]
        order_number = _first_order_number(
            first.receiving_document.order_number if first.receiving_document else None
        )
        # ИНН поставщика (из справочника Supplier, заполняется при загрузке
        # накладной — см. receiving._find_or_create_supplier) — надежный
        # уникальный идентификатор для поиска контрагента в 1С, в отличие
        # от сравнения по названию: "Наименование" контрагента в 1С часто
        # внутренний короткий алиас, а не то, что написано в накладной,
        # так что текстовое совпадение с supplier_name может не найтись
        # вовсе (см. SyncWMS.bsl НайтиКонтрагентаПоИНН/ПоИмени).
        supplier_inn = None
        if first.receiving_document and first.receiving_document.supplier_ref:
            supplier_inn = first.receiving_document.supplier_ref.inn
        payloads.append(
            {
                "id": receiving_document_id,
                "invoice_number": first.invoice_number,
                # Номер "Заказа поставщику" в 1С (вносится вручную в WMS при
                # загрузке накладной, см. ReceivingDocument.order_number) —
                # именно по нему 1С ищет основание для возврата (см.
                # SyncWMS.bsl НайтиЗаказПоставщикуПоНомеру), а не по
                # invoice_number, который относится к документу поступления,
                # а не к заказу поставщику.
                "order_number": order_number or "",
                "supplier": first.supplier_name or "",
                "supplier_inn": supplier_inn or "",
                "warehouse": first.warehouse.name if first.warehouse else "",
                "comment": f"WMS: возврат по приемке {first.invoice_number}",
                "lines": [
                    {
                        "barcode": r.nomenclature.barcode,
                        "name": r.nomenclature.name,
                        "qty": r.qty,
                    }
                    for r in group
                ],
            }
        )
    return payloads


def _movement_corrections_candidates():
    """Уже выгруженные в 1С перемещения (synced_to_1c_at заполнен), состав
    которых потом поменяли — добавили/удалили короб (movement.add_box/
    delete_line) или поправили количество в уже уехавшем коробе (boxes.
    add_item/update_item/move_item/delete_item), см.
    MovementDocument.composition_changed_at. В отличие от корректировки
    приемки, искать документ в 1С приходится не по отдельному реквизиту
    (у перемещения такого нет), а по номеру WMS, который зашит в текст
    "Комментарий" при создании (см. _movement_payload) — см. SyncWMS.bsl
    НайтиПеремещениеПоНомеруWMS."""
    return (
        MovementDocument.query.filter(
            MovementDocument.synced_to_1c_at.isnot(None),
            MovementDocument.composition_changed_at.isnot(None),
        )
        .order_by(MovementDocument.id)
        .all()
    )


def _movement_corrections_export():
    """Полный актуальный состав документа (не дельта) — 1С проще целиком
    пересобрать табличную часть под этот список (см. SyncWMS.bsl
    СкорректироватьПеремещение), чем разбирать построчные диффы."""
    payloads = []
    for doc in _movement_corrections_candidates():
        lines = []
        for line in doc.lines:
            for item in line.box.items:
                lines.append(
                    {
                        "barcode": item.nomenclature.barcode,
                        "name": item.nomenclature.name,
                        "qty": item.qty,
                    }
                )
        payloads.append(
            {
                "id": doc.id,
                "number": doc.number,
                "lines": lines,
            }
        )
    return payloads


def _receiving_adjustments_export():
    """Приемки из накладной (см. is_from_invoice_import), где пересчет уже
    завершен (пересчет меняет qty только в статусах draft/recounting — см.
    receiving.confirm_line — значит после них цифры больше не изменятся) и
    есть расхождение хотя бы по одной строке (qty != expected_qty) — 1С
    должна поправить "Количество" в уже заведенной приходной накладной под
    фактически принятое (см. SyncWMS.bsl СкорректироватьПриемку). Она НЕ
    трогает проведение документа — если накладная уже проведена, поправить
    количество должен бухгалтер вручную (сама 1С-обработка это пропустит и
    напишет предупреждение в диагностику).

    Еще не выгруженные — recount_synced_to_1c_at пусто; выгружаем ЦЕЛИКОМ
    актуальные qty по всем строкам документа (не только расходящимся) —
    проще сопоставить в 1С один раз, чем помнить, какие строки уже
    поправлены."""
    payloads = []
    for doc, _diff_lines in _receiving_adjustments_candidates():
        lines = doc.lines.all()
        payloads.append(
            {
                "id": doc.id,
                "invoice_number": doc.number,
                "lines": [
                    {
                        "barcode": line.nomenclature.barcode,
                        "name": line.nomenclature.name,
                        "qty": line.qty,
                    }
                    for line in lines
                ],
            }
        )
    return payloads


def _receiving_adjustments_candidates():
    """(doc, diff_lines) для документов, которые реально попадут в
    _receiving_adjustments_export — используется и там, и в списке очереди
    выгрузки (pending()), чтобы список на экране совпадал с тем, что
    отправится в 1С."""
    documents = (
        ReceivingDocument.query.filter(
            ReceivingDocument.invoice_file_name.isnot(None),
            ReceivingDocument.status.in_(("sorting", "completed")),
            ReceivingDocument.recount_synced_to_1c_at.is_(None),
            ReceivingDocument.accounting_entered_at.is_(None),
        )
        .order_by(ReceivingDocument.id)
        .all()
    )
    result = []
    for doc in documents:
        diff_lines = [
            line for line in doc.lines.all()
            if line.expected_qty is not None and line.qty != line.expected_qty
        ]
        if diff_lines:
            result.append((doc, diff_lines))
    return result


@bp.route("/api/reconciliation")
def reconciliation():
    """Отдает актуальный состав уже существующих документов для сверки.

    В отличие от /api/export этот метод ничего не создает и не меняет в
    1С. Внешняя обработка находит ранее созданные документы, читает их
    текущий состав и возвращает сравнение через обычный export/confirm.
    """
    if not _check_token():
        return jsonify({"ok": False, "error": "Неверный или отсутствующий токен"}), 401

    today = datetime.utcnow().date()
    try:
        date_from = datetime.strptime(
            request.args.get("date_from") or (today - timedelta(days=30)).isoformat(),
            "%Y-%m-%d",
        ).date()
        date_to = datetime.strptime(
            request.args.get("date_to") or today.isoformat(), "%Y-%m-%d"
        ).date()
    except ValueError:
        return jsonify({"ok": False, "error": "Даты должны иметь формат ГГГГ-ММ-ДД"}), 400
    if date_from > date_to:
        return jsonify({"ok": False, "error": "Дата начала позже даты окончания"}), 400

    period_start = datetime.combine(date_from, time.min)
    # Верхняя граница исключающая: так дата «по» входит целиком, включая
    # документы, завершенные в 23:59:59.
    period_end = datetime.combine(date_to + timedelta(days=1), time.min)
    movement_date = func.coalesce(MovementDocument.completed_at, MovementDocument.created_at)
    receiving_date = func.coalesce(ReceivingDocument.completed_at, ReceivingDocument.created_at)

    movements = (
        MovementDocument.query.filter(
            MovementDocument.synced_to_1c_at.isnot(None),
            movement_date >= period_start,
            movement_date < period_end,
        )
        .order_by(MovementDocument.id)
        .all()
    )
    receivings = (
        ReceivingDocument.query.filter(
            ReceivingDocument.invoice_file_name.isnot(None),
            ReceivingDocument.status.in_(("sorting", "completed")),
            receiving_date >= period_start,
            receiving_date < period_end,
        )
        .order_by(ReceivingDocument.id)
        .all()
    )
    return jsonify(
        {
            "ok": True,
            "period": {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
            "movements": [_movement_reconciliation_payload(doc) for doc in movements],
            "receivings": [_receiving_reconciliation_payload(doc) for doc in receivings],
        }
    )


@bp.route("/api/export")
def export():
    """Отдает документы, готовые к переносу в 1С: перемещение — только когда
    в WMS дошло до статуса "Создана заявка" (сохранен номер заявки и
    заполнен marketplace_request_created_at) — на сборке/собрано еще рано,
    а ждать "Отгружено" (кнопка
    "Принято на складе", doc.received_at) не нужно: как только заявка на
    маркетплейс создана, документ уже достаточно определен для 1С.
    Инвентаризация — завершенные. Уже выгруженные (synced_to_1c_at заполнен)
    не отдаются повторно — 1С подтверждает получение через export/confirm.
    Перемещения, которые бухгалтер уже отметил галочкой "внесено в 1С"
    вручную (accounting_entered_at заполнен), тоже не отдаются — он ведет их
    отдельно и повторный автоматический перенос задвоил бы документ."""
    if not _check_token():
        return jsonify({"ok": False, "error": "Неверный или отсутствующий токен"}), 401

    movements = _pending_movements_query().order_by(MovementDocument.id).all()
    inventories = _pending_inventories_query().order_by(InventoryDocument.id).all()

    return jsonify(
        {
            "ok": True,
            "movements": [_movement_payload(d) for d in movements],
            "inventories": [_inventory_payload(d) for d in inventories],
            "supplier_returns": _supplier_returns_export(),
            "receiving_adjustments": _receiving_adjustments_export(),
            "movement_corrections": _movement_corrections_export(),
        }
    )


@bp.route("/api/export/confirm", methods=["POST"])
def export_confirm():
    """1С вызывает это после того, как реально создала у себя документы —
    подтвержденные номера помечаются выгруженными и больше не попадут в
    /api/export. Без этого шага повторное нажатие "Синхронизировать"
    задвоило бы документы в 1С."""
    if not _check_token():
        return jsonify({"ok": False, "error": "Неверный или отсутствующий токен"}), 401

    data = request.get_json(silent=True) or {}
    movement_ids = data.get("movement_ids") or []
    inventory_ids = data.get("inventory_ids") or []
    supplier_return_ids = data.get("supplier_return_ids") or []
    receiving_adjustment_ids = data.get("receiving_adjustment_ids") or []
    # {str(movement_id): "текст предупреждения"} — часть строк документа не
    # сопоставилась с номенклатурой в 1С и была пропущена (см. SyncWMS.bsl
    # СоздатьПеремещениеТоваров); документ при этом всё равно создан и
    # подтвержден, только не полностью — показываем "!" в списке.
    movement_warnings = data.get("movement_warnings") or {}

    # 1С может вернуть фактически записанные количества строк. Сохраняем
    # только расхождения; повторная сверка полностью заменяет результат по
    # документу, поэтому старое предупреждение не остается висеть.
    quantity_checks = data.get("quantity_checks") or []
    checked_documents = set()
    quantity_mismatches = 0
    for check in quantity_checks:
        document_type = str(check.get("document_type") or "movement")[:30]
        document_id = int(check.get("document_id") or 0)
        if not document_id:
            continue
        key = (document_type, document_id)
        if key not in checked_documents:
            OneCQuantityCheck.query.filter_by(
                document_type=document_type, document_id=document_id
            ).delete(synchronize_session=False)
            checked_documents.add(key)
        wms_qty = float(check.get("wms_qty") or 0)
        one_c_qty = float(check.get("one_c_qty") or 0)
        if abs(wms_qty - one_c_qty) < 0.000001:
            continue
        db.session.add(
            OneCQuantityCheck(
                document_type=document_type,
                document_id=document_id,
                document_number=str(check.get("document_number") or document_id),
                barcode=str(check.get("barcode") or "") or None,
                item_name=str(check.get("name") or "") or None,
                wms_qty=wms_qty,
                one_c_qty=one_c_qty,
            )
        )
        quantity_mismatches += 1

    now = datetime.utcnow()
    confirmed_movements = (
        MovementDocument.query.filter(
            MovementDocument.id.in_(movement_ids), MovementDocument.synced_to_1c_at.is_(None)
        ).all()
    )
    for doc in confirmed_movements:
        doc.synced_to_1c_at = now
        if doc.accounting_entered_at is None:
            doc.accounting_entered_at = now
        doc.sync_warning = movement_warnings.get(str(doc.id))

    confirmed_inventories = (
        InventoryDocument.query.filter(
            InventoryDocument.id.in_(inventory_ids), InventoryDocument.synced_to_1c_at.is_(None)
        ).all()
    )
    for doc in confirmed_inventories:
        doc.synced_to_1c_at = now

    # supplier_return_ids — это receiving_document_id (см. _supplier_returns_export,
    # где id документа для 1С — это id приемки, объединяющий несколько строк
    # возврата), а не id отдельных SupplierReturn.
    confirmed_returns = (
        SupplierReturn.query.filter(
            SupplierReturn.receiving_document_id.in_(supplier_return_ids),
            SupplierReturn.synced_to_1c_at.is_(None),
        ).all()
    )
    for ret in confirmed_returns:
        ret.synced_to_1c_at = now

    # id здесь — id приемки (см. _receiving_adjustments_export), а не строк.
    # 1С присылает сюда только те id, которые реально поправила — если
    # накладная уже проведена (см. SyncWMS.bsl СкорректироватьПриемку),
    # документ остается в ошибках 1С и НЕ попадает в этот список, поэтому
    # WMS продолжит присылать его на каждой синхронизации, пока бухгалтер
    # не поправит накладную вручную и корректировка не пройдет успешно.
    confirmed_receiving_adjustments = (
        ReceivingDocument.query.filter(
            ReceivingDocument.id.in_(receiving_adjustment_ids),
            ReceivingDocument.recount_synced_to_1c_at.is_(None),
        ).all()
    )
    for doc in confirmed_receiving_adjustments:
        doc.recount_synced_to_1c_at = now

    # id здесь — id перемещения (см. _movement_corrections_export). Сбрасываем
    # только composition_changed_at — само перемещение уже выгружено раньше
    # (synced_to_1c_at не трогаем), просто состав в 1С теперь снова
    # актуальный. Если 1С не смогла поправить документ (например, он уже
    # проведен — см. SyncWMS.bsl СкорректироватьПеремещение), она не пришлет
    # его id сюда, и WMS продолжит предлагать коррекцию на каждой
    # синхронизации.
    movement_correction_ids = data.get("movement_correction_ids") or []
    confirmed_movement_corrections = (
        MovementDocument.query.filter(
            MovementDocument.id.in_(movement_correction_ids),
            MovementDocument.composition_changed_at.isnot(None),
        ).all()
    )
    for doc in confirmed_movement_corrections:
        doc.composition_changed_at = None

    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "confirmed": {
                "movements": len(confirmed_movements),
                "inventories": len(confirmed_inventories),
                "supplier_returns": len(confirmed_returns),
                "receiving_adjustments": len(confirmed_receiving_adjustments),
                "movement_corrections": len(confirmed_movement_corrections),
                "quantity_mismatches": quantity_mismatches,
            },
        }
    )


@bp.route("/pending")
def pending():
    """Полные списки того, что ждет выгрузки в 1С (см. настройки — там
    только счетчики), с возможностью убрать конкретный документ из очереди
    вручную (см. toggle_* ниже) — например, если бухгалтер уже внес его в
    1С сам, минуя автоматическую синхронизацию."""
    guard = _admin_required()
    if guard:
        return guard

    movements = _pending_movements_query().order_by(MovementDocument.completed_at.desc()).all()
    inventories = _pending_inventories_query().order_by(InventoryDocument.completed_at.desc()).all()
    receiving_adjustments = _receiving_adjustments_candidates()
    supplier_return_groups = _supplier_returns_export()
    movement_corrections = _movement_corrections_candidates()

    return render_template(
        "integration_1c/pending.html",
        movements=movements,
        inventories=inventories,
        receiving_adjustments=receiving_adjustments,
        supplier_return_groups=supplier_return_groups,
        movement_corrections=movement_corrections,
    )


@bp.route("/pending/movement/<int:doc_id>/toggle", methods=["POST"])
def toggle_movement(doc_id):
    guard = _admin_required()
    if guard:
        return guard

    doc = MovementDocument.query.get_or_404(doc_id)
    doc.accounting_entered_at = None if doc.accounting_entered_at else datetime.utcnow()
    db.session.commit()
    if doc.accounting_entered_at:
        flash(f"Перемещение {doc.number} убрано из очереди выгрузки в 1С", "success")
    else:
        flash(f"Перемещение {doc.number} возвращено в очередь выгрузки", "success")
    return redirect(url_for("integration_1c.pending"))


@bp.route("/pending/inventory/<int:doc_id>/toggle", methods=["POST"])
def toggle_inventory(doc_id):
    guard = _admin_required()
    if guard:
        return guard

    doc = InventoryDocument.query.get_or_404(doc_id)
    doc.accounting_entered_at = None if doc.accounting_entered_at else datetime.utcnow()
    db.session.commit()
    if doc.accounting_entered_at:
        flash(f"Инвентаризация {doc.number} убрана из очереди выгрузки в 1С", "success")
    else:
        flash(f"Инвентаризация {doc.number} возвращена в очередь выгрузки", "success")
    return redirect(url_for("integration_1c.pending"))


@bp.route("/pending/receiving-adjustment/<int:doc_id>/toggle", methods=["POST"])
def toggle_receiving_adjustment(doc_id):
    guard = _admin_required()
    if guard:
        return guard

    doc = ReceivingDocument.query.get_or_404(doc_id)
    doc.accounting_entered_at = None if doc.accounting_entered_at else datetime.utcnow()
    db.session.commit()
    if doc.accounting_entered_at:
        flash(f"Корректировка по приемке {doc.number} убрана из очереди выгрузки в 1С", "success")
    else:
        flash(f"Корректировка по приемке {doc.number} возвращена в очередь выгрузки", "success")
    return redirect(url_for("integration_1c.pending"))


@bp.route("/pending/movement-correction/<int:doc_id>/toggle", methods=["POST"])
def toggle_movement_correction(doc_id):
    """Убрать перемещение из очереди коррекции — бухгалтер поправил документ
    в 1С сам, минуя автовыгрузку. В отличие от остальных toggle_* здесь нет
    "вернуть обратно": composition_changed_at либо уже отражает реальное
    расхождение (и ставится заново само, стоит еще раз поменять состав —
    см. movement.add_box/delete_line, boxes.*), либо расхождения нет вовсе."""
    guard = _admin_required()
    if guard:
        return guard

    doc = MovementDocument.query.get_or_404(doc_id)
    if doc.composition_changed_at is None:
        flash("У этого перемещения нет несинхронизированных изменений", "danger")
        return redirect(url_for("integration_1c.pending"))

    doc.composition_changed_at = None
    db.session.commit()
    flash(f"Перемещение {doc.number} убрано из очереди коррекции 1С", "success")
    return redirect(url_for("integration_1c.pending"))


@bp.route("/pending/supplier-return/<int:receiving_document_id>/toggle", methods=["POST"])
def toggle_supplier_return(receiving_document_id):
    """Возврат выгружается в 1С одним документом на всю приемку сразу (см.
    _supplier_returns_export) — значит и исключать из очереди нужно все
    строки возврата этой приемки разом, а не по одной."""
    guard = _admin_required()
    if guard:
        return guard

    returns = SupplierReturn.query.filter(
        SupplierReturn.receiving_document_id == receiving_document_id,
        SupplierReturn.invoice_number.isnot(None),
        SupplierReturn.synced_to_1c_at.is_(None),
    ).all()
    if not returns:
        flash("Возвраты по этой приемке не найдены или уже выгружены", "danger")
        return redirect(url_for("integration_1c.pending"))

    excluding = any(r.accounting_entered_at is None for r in returns)
    now = datetime.utcnow() if excluding else None
    for r in returns:
        r.accounting_entered_at = now
    db.session.commit()

    invoice_number = returns[0].invoice_number
    if excluding:
        flash(f"Возврат по приемке {invoice_number} убран из очереди выгрузки в 1С", "success")
    else:
        flash(f"Возврат по приемке {invoice_number} возвращен в очередь выгрузки", "success")
    return redirect(url_for("integration_1c.pending"))
