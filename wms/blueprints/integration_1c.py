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
from ..models import AppSetting, InventoryDocument, MovementDocument

bp = Blueprint("integration_1c", __name__)

TOKEN_KEY = "api_1c_token"

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
        .filter(MovementDocument.received_at.isnot(None))
        .count()
    )
    pending_inventories = InventoryDocument.query.filter_by(
        status="completed", synced_to_1c_at=None
    ).count()

    return render_template(
        "integration_1c/settings.html",
        token=_get_token(),
        pending_movements=pending_movements,
        pending_inventories=pending_inventories,
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
    return {
        "id": doc.id,
        "number": doc.number,
        "date": (doc.completed_at or doc.created_at).isoformat(),
        "from_warehouse": doc.from_warehouse.name if doc.from_warehouse else "",
        "to_warehouse": doc.to_warehouse.name if doc.to_warehouse else "",
        "comment": f"WMS: {doc.number}",
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


@bp.route("/api/export")
def export():
    """Отдает документы, готовые к переносу в 1С: перемещение — только
    завершенные и уже принятые на складе назначения (received_at заполнен —
    иначе выгрузили бы то, что по факту еще в пути); инвентаризация —
    завершенные. Уже выгруженные (synced_to_1c_at заполнен) не отдаются
    повторно — 1С подтверждает получение через export/confirm."""
    if not _check_token():
        return jsonify({"ok": False, "error": "Неверный или отсутствующий токен"}), 401

    movements = (
        MovementDocument.query.filter_by(status="completed", synced_to_1c_at=None)
        .filter(MovementDocument.received_at.isnot(None))
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

    now = datetime.utcnow()
    confirmed_movements = (
        MovementDocument.query.filter(
            MovementDocument.id.in_(movement_ids), MovementDocument.synced_to_1c_at.is_(None)
        ).all()
    )
    for doc in confirmed_movements:
        doc.synced_to_1c_at = now

    confirmed_inventories = (
        InventoryDocument.query.filter(
            InventoryDocument.id.in_(inventory_ids), InventoryDocument.synced_to_1c_at.is_(None)
        ).all()
    )
    for doc in confirmed_inventories:
        doc.synced_to_1c_at = now

    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "confirmed": {
                "movements": len(confirmed_movements),
                "inventories": len(confirmed_inventories),
            },
        }
    )
