from flask import Blueprint, render_template
from flask_login import login_required

bp = Blueprint("onboarding", __name__)


@bp.route("/")
@login_required
def guide():
    """Обучающая страница «Первый день на складе» — своя страница сайта
    (не внешняя ссылка), доступна любому вошедшему сотруднику независимо
    от роли и ограничений по разделам меню (см. before_request в
    wms/__init__.py)."""
    return render_template("onboarding/guide.html")
