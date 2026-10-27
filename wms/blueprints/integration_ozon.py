"""Настройки интеграции с Ozon Seller API — хранение Client-Id/Api-Key,
по тому же принципу "ключ-значение" в AppSetting, что и токен 1С (см.
integration_1c.TOKEN_KEY). Сами вызовы API — в utils.ozon_api."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import AppSetting

bp = Blueprint("integration_ozon", __name__)

CLIENT_ID_KEY = "ozon_client_id"
API_KEY_KEY = "ozon_api_key"


def _get_setting(key):
    setting = AppSetting.query.get(key)
    return setting.value if setting else None


def _set_setting(key, value):
    setting = AppSetting.query.get(key)
    if not setting:
        setting = AppSetting(key=key)
        db.session.add(setting)
    setting.value = value


def get_ozon_credentials():
    """(client_id, api_key) — оба None, если ключ еще не сохранен."""
    return _get_setting(CLIENT_ID_KEY), _get_setting(API_KEY_KEY)


@bp.route("/", methods=["GET", "POST"])
def settings():
    if not current_user.is_admin:
        flash("Настройки интеграции с Ozon доступны только администратору", "danger")
        return redirect(url_for("main.index"))

    if request.method == "POST":
        client_id = request.form.get("client_id", "").strip()
        api_key = request.form.get("api_key", "").strip()
        if not client_id or not api_key:
            flash("Укажите и Client-Id, и Api-Key", "danger")
            return redirect(url_for("integration_ozon.settings"))
        _set_setting(CLIENT_ID_KEY, client_id)
        _set_setting(API_KEY_KEY, api_key)
        db.session.commit()
        flash("Ключ Ozon API сохранен", "success")
        return redirect(url_for("integration_ozon.settings"))

    client_id, api_key = get_ozon_credentials()
    return render_template("integration_ozon/settings.html", client_id=client_id, api_key=api_key)
