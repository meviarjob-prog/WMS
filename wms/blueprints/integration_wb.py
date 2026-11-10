"""Настройки интеграции с WB API — токен хранится в AppSetting (как ключ
Ozon, см. integration_ozon). Сами вызовы — в utils.wb_api."""

import base64
import json
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import AppSetting
from ..utils.wb_api import WbApiError, wb_request

bp = Blueprint("integration_wb", __name__)

TOKEN_KEY = "wb_api_token"

# Биты категорий в поле "s" токена WB (см. документацию WB, «Авторизация»).
_SCOPE_BITS = {
    1: "Контент", 2: "Аналитика", 3: "Цены и скидки", 4: "Маркетплейс", 5: "Статистика",
    6: "Продвижение", 7: "Вопросы и отзывы", 9: "Чат с покупателями", 10: "Поставки",
    11: "Возвраты", 12: "Документы", 13: "Финансы", 16: "Пользователи",
}
_TOKEN_TYPES = {1: "базовый", 2: "тестовый", 3: "персональный", 4: "сервисный"}
SUPPLIES_BIT = 10
READ_ONLY_BIT = 30


def get_wb_token():
    setting = AppSetting.query.get(TOKEN_KEY)
    return (setting.value or "").strip() or None if setting else None


def describe_token(token):
    """Расшифровка открытой части токена (без проверки подписи) — тип,
    категории, срок. Ничего никуда не отправляет."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (IndexError, ValueError):
        return None
    scope = int(payload.get("s") or 0)
    exp = payload.get("exp")
    return {
        "type": _TOKEN_TYPES.get(payload.get("acc"), "неизвестный"),
        "categories": [name for bit, name in _SCOPE_BITS.items() if scope >> bit & 1],
        "has_supplies": bool(scope >> SUPPLIES_BIT & 1),
        "read_only": bool(scope >> READ_ONLY_BIT & 1),
        "expires": datetime.utcfromtimestamp(exp) if exp else None,
        "test": bool(payload.get("t")),
    }


@bp.route("/", methods=["GET", "POST"])
def settings():
    if not current_user.is_admin:
        flash("Настройки интеграции с WB доступны только администратору", "danger")
        return redirect(url_for("main.index"))
    if request.method == "POST":
        token = "".join(request.form.get("token", "").split())
        if not token:
            flash("Вставьте токен WB API", "danger")
            return redirect(url_for("integration_wb.settings"))
        info = describe_token(token)
        if info is None:
            flash("Это не похоже на токен WB API — проверьте, что скопировали его целиком", "danger")
            return redirect(url_for("integration_wb.settings"))
        setting = AppSetting.query.get(TOKEN_KEY)
        if setting is None:
            setting = AppSetting(key=TOKEN_KEY)
            db.session.add(setting)
        setting.value = token
        db.session.commit()
        flash("Токен WB API сохранен", "success")
        if not info["has_supplies"]:
            flash("В токене нет категории «Поставки» — проверка приемки WB с ним работать не будет", "warning")
        return redirect(url_for("integration_wb.settings"))
    token = get_wb_token()
    return render_template(
        "integration_wb/settings.html", has_token=bool(token),
        token_tail=token[-6:] if token else "", info=describe_token(token) if token else None,
    )


@bp.route("/check", methods=["POST"])
def check():
    if not current_user.is_admin:
        flash("Настройки интеграции с WB доступны только администратору", "danger")
        return redirect(url_for("main.index"))
    try:
        data = wb_request(get_wb_token(), "GET", "/ping", timeout=15, max_retries=1)
    except WbApiError as exc:
        flash(f"WB API: {exc}", "danger")
        return redirect(url_for("integration_wb.settings"))
    flash(f"Токен работает, сервис поставок WB отвечает ({(data or {}).get('Status', 'OK')})", "success")
    return redirect(url_for("integration_wb.settings"))
