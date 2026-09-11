"""Обмен с внешней обработкой 1С: выгрузка завершенных документов
перемещения и инвентаризации, которых 1С еще не забирала.

Аутентификация — отдельным токеном (не логином/паролем пользователя WMS),
потому что запрос идет не из браузера, а из 1С по HTTP. Токен сравнивается
с тем, что администратор сгенерировал на странице настроек этой
интеграции; эти два route (export/export_confirm) сознательно исключены
из общей проверки логина в wms/__init__.py — см. API_1C_PUBLIC_ENDPOINTS.
"""

import secrets
from datetime import datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import AppSetting, InventoryDocument, MovementDocument, SupplierReturn

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
    from_name = doc.from_warehouse.name if doc.from_warehouse else ""
    to_name = doc.to_warehouse.name if doc.to_warehouse else ""
    if (
        from_name in DIRECT_TRANSFER_WAREHOUSE_NAMES
        and to_name in DIRECT_TRANSFER_WAREHOUSE_NAMES
    ):
        return to_name
    return FULFILLMENT_WAREHOUSE_NAME

# Именно эти два endpoint'а обмена с 1С не требуют логина в WMS — только
# токен (см. проверку в самих view). Импортируется в wms/__init__.py.
API_1C_PUBLIC_ENDPOINTS = {"integration_1c.export", "integration_1c.export_confirm"}


def _get_token():
    setting = AppSetting.query.get(TOKEN_KEY)
    return setting.value if setting else None


def _check_token():
    token = request.headers.get("X-1C-Token") or request.args.get("token")
    expected = _get_token()
    return bool(expected) and token == expected


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

    pending_movements = (
        MovementDocument.query.filter_by(status="completed", synced_to_1c_at=None)
        .filter(MovementDocument.accounting_entered_at.is_(None))
        .count()
    )
    pending_inventories = InventoryDocument.query.filter_by(
        status="completed", synced_to_1c_at=None
    ).count()
    pending_supplier_returns = SupplierReturn.query.filter(
        SupplierReturn.synced_to_1c_at.is_(None),
        SupplierReturn.invoice_number.isnot(None),
    ).count()

    return render_template(
        "integration_1c/settings.html",
        token=_get_token(),
        pending_movements=pending_movements,
        pending_inventories=pending_inventories,
        pending_supplier_returns=pending_supplier_returns,
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
        "comment": f"WMS: {doc.number} (склад получатель: {to_warehouse_name})",
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
        order_number = first.receiving_document.order_number if first.receiving_document else None
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


@bp.route("/api/export")
def export():
    """Отдает документы, готовые к переносу в 1С: перемещение — сразу как
    завершено в WMS (кнопка "Завершить перемещение"), не дожидаясь "Принято
    на складе" — в 1С документ "Перемещение товаров" как раз и отражает,
    что товар в пути; отдельный документ по факту приемки на складе
    назначения бухгалтерия заводит в 1С вручную. Инвентаризация —
    завершенные. Уже выгруженные (synced_to_1c_at заполнен) не отдаются
    повторно — 1С подтверждает получение через export/confirm. Перемещения,
    которые бухгалтер уже отметил галочкой "внесено в 1С" вручную
    (accounting_entered_at заполнен), тоже не отдаются — он ведет их отдельно
    и повторный автоматический перенос задвоил бы документ."""
    if not _check_token():
        return jsonify({"ok": False, "error": "Неверный или отсутствующий токен"}), 401

    movements = (
        MovementDocument.query.filter_by(status="completed", synced_to_1c_at=None)
        .filter(MovementDocument.accounting_entered_at.is_(None))
        .order_by(MovementDocument.id)
        .all()
    )
    inventories = (
        InventoryDocument.query.filter_by(status="completed", synced_to_1c_at=None)
        .order_by(InventoryDocument.id)
        .all()
    )

    return jsonify(
        {
            "ok": True,
            "movements": [_movement_payload(d) for d in movements],
            "inventories": [_inventory_payload(d) for d in inventories],
            "supplier_returns": _supplier_returns_export(),
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

    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "confirmed": {
                "movements": len(confirmed_movements),
                "inventories": len(confirmed_inventories),
                "supplier_returns": len(confirmed_returns),
            },
        }
    )
