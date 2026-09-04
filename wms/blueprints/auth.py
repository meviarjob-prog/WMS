import secrets

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..models import User

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    if request.method == "GET":
        return render_template("auth/login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    user = User.query.filter_by(username=username).first()
    if not user or not user.is_active_user or not user.check_password(password):
        flash("Неверный логин или пароль", "danger")
        return render_template("auth/login.html", username=username)

    login_user(user, remember=True)
    next_url = request.args.get("next")
    return redirect(next_url or url_for("main.index"))


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Вы вышли из системы", "success")
    return redirect(url_for("auth.login"))


def _require_admin():
    if not current_user.is_admin:
        flash("Доступно только администраторам", "danger")
        return False
    return True


@bp.route("/users")
@login_required
def users():
    if not _require_admin():
        return redirect(url_for("main.index"))
    all_users = User.query.order_by(User.username).all()
    return render_template("auth/users.html", users=all_users)


@bp.route("/users/create", methods=["POST"])
@login_required
def create_user():
    if not _require_admin():
        return redirect(url_for("main.index"))

    username = request.form.get("username", "").strip()
    full_name = request.form.get("full_name", "").strip()
    is_admin = request.form.get("is_admin") == "on"

    if not username:
        flash("Укажите логин", "danger")
        return redirect(url_for("auth.users"))

    if User.query.filter_by(username=username).first():
        flash(f"Пользователь '{username}' уже существует", "danger")
        return redirect(url_for("auth.users"))

    temp_password = secrets.token_urlsafe(6)
    user = User(username=username, full_name=full_name, is_admin=is_admin)
    user.set_password(temp_password)
    db.session.add(user)
    db.session.commit()

    flash(
        f"Пользователь «{username}» создан. Временный пароль: {temp_password} "
        f"— сообщите его пользователю, он сможет сменить пароль после входа.",
        "success",
    )
    return redirect(url_for("auth.users"))


@bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@login_required
def toggle_user(user_id):
    if not _require_admin():
        return redirect(url_for("main.index"))

    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("Нельзя отключить самого себя", "danger")
        return redirect(url_for("auth.users"))

    user.is_active_user = not user.is_active_user
    db.session.commit()
    return redirect(url_for("auth.users"))


@bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
def reset_password(user_id):
    if not _require_admin():
        return redirect(url_for("main.index"))

    user = User.query.get_or_404(user_id)
    temp_password = secrets.token_urlsafe(6)
    user.set_password(temp_password)
    db.session.commit()
    flash(f"Новый временный пароль для «{user.username}»: {temp_password}", "success")
    return redirect(url_for("auth.users"))


@bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "GET":
        return render_template("auth/change_password.html")

    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    new_password2 = request.form.get("new_password2", "")

    if not current_user.check_password(current_password):
        flash("Текущий пароль указан неверно", "danger")
        return render_template("auth/change_password.html")

    if len(new_password) < 4:
        flash("Новый пароль слишком короткий (минимум 4 символа)", "danger")
        return render_template("auth/change_password.html")

    if new_password != new_password2:
        flash("Пароли не совпадают", "danger")
        return render_template("auth/change_password.html")

    current_user.set_password(new_password)
    db.session.commit()
    flash("Пароль изменен", "success")
    return redirect(url_for("main.index"))
