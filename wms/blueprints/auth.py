import secrets

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..models import SECTIONS, User, Warehouse

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
    session["session_version"] = user.session_version or 0
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
    """Страница «Настройки» администратора: управление пользователями и
    доступом к разделам, плюс получатели и отправитель для стикеров
    отправления перемещений (см. warehouses.update_recipient и
    movement.update_shipping_label_sender) — все административные
    настройки в одном месте, вместо разбросанных по разным разделам."""
    if not _require_admin():
        return redirect(url_for("main.index"))
    from .movement import get_shipping_label_sender_override

    all_users = User.query.order_by(User.username).all()
    warehouses = Warehouse.query.order_by(Warehouse.code).all()

    return render_template(
        "auth/users.html",
        users=all_users,
        sections=SECTIONS,
        warehouses=warehouses,
        shipping_label_sender=get_shipping_label_sender_override(),
    )


@bp.route("/users/create", methods=["POST"])
@login_required
def create_user():
    if not _require_admin():
        return redirect(url_for("main.index"))

    username = request.form.get("username", "").strip()
    full_name = request.form.get("full_name", "").strip()
    is_admin = request.form.get("is_admin") == "on"
    shift_minutes = request.form.get("shift_minutes", type=int) or 480
    role = request.form.get("role", "warehouse")
    warehouse_id = request.form.get("warehouse_id", type=int)
    if role not in ("warehouse", "production"):
        role = "warehouse"

    if not username:
        flash("Укажите логин", "danger")
        return redirect(url_for("auth.users"))

    if User.query.filter_by(username=username).first():
        flash(f"Пользователь '{username}' уже существует", "danger")
        return redirect(url_for("auth.users"))

    temp_password = secrets.token_urlsafe(6)
    user = User(
        username=username,
        full_name=full_name,
        is_admin=is_admin,
        shift_minutes=shift_minutes,
        role=role,
        warehouse_id=warehouse_id,
    )
    user.set_password(temp_password)
    db.session.add(user)
    db.session.commit()

    flash(
        f"Пользователь «{username}» создан. Временный пароль: {temp_password} "
        f"— сообщите его пользователю, он сможет сменить пароль после входа.",
        "success",
    )
    return redirect(url_for("auth.users"))


@bp.route("/users/<int:user_id>/toggle-admin", methods=["POST"])
@login_required
def toggle_admin(user_id):
    """Права администратора (полный доступ ко всем разделам и функциям
    независимо от role/allowed_sections, см. User.is_admin) — раньше
    ставились только при создании пользователя (чекбокс в форме выше),
    поменять их существующему пользователю было нельзя вообще. Нельзя
    менять собственные права — этим гарантируется, что администратор,
    выполняющий действие, сам остается администратором, то есть хотя бы
    один администратор в системе есть всегда."""
    if not _require_admin():
        return redirect(url_for("main.index"))

    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("Нельзя изменить права администратора у самого себя — попросите другого администратора", "danger")
        return redirect(url_for("auth.users"))

    user.is_admin = not user.is_admin
    db.session.commit()
    flash(
        f"Права администратора для «{user.username}» {'выданы' if user.is_admin else 'сняты'}",
        "success",
    )
    return redirect(url_for("auth.users"))


@bp.route("/users/<int:user_id>/shift-minutes", methods=["POST"])
@login_required
def update_shift_minutes(user_id):
    """Плановая длительность смены (мин) — используется для расчета
    эффективности в отчете модуля «Производство»."""
    if not _require_admin():
        return redirect(url_for("main.index"))

    user = User.query.get_or_404(user_id)
    shift_minutes = request.form.get("shift_minutes", type=int)
    if not shift_minutes or shift_minutes <= 0:
        flash("Укажите корректную длительность смены в минутах", "danger")
        return redirect(url_for("auth.users"))

    user.shift_minutes = shift_minutes
    db.session.commit()
    flash(f"Плановая смена для «{user.username}» обновлена: {shift_minutes} мин", "success")
    return redirect(url_for("auth.users"))


@bp.route("/users/<int:user_id>/role", methods=["POST"])
@login_required
def update_role(user_id):
    """Роль ограничивает доступ: "production" видит только сканирование ЧЗ
    на производстве, ничего больше (проверяется в before_request)."""
    if not _require_admin():
        return redirect(url_for("main.index"))

    user = User.query.get_or_404(user_id)
    role = request.form.get("role", "warehouse")
    if role not in ("warehouse", "production"):
        flash("Некорректная роль", "danger")
        return redirect(url_for("auth.users"))

    if user.id == current_user.id and role == "production" and not user.is_admin:
        flash("Нельзя ограничить самого себя до роли «производство»", "danger")
        return redirect(url_for("auth.users"))

    user.role = role
    db.session.commit()
    flash(f"Роль для «{user.username}» обновлена", "success")
    return redirect(url_for("auth.users"))


@bp.route("/users/<int:user_id>/sections", methods=["POST"])
@login_required
def update_sections(user_id):
    """Точечный доступ к разделам (см. User.allowed_sections) — отдельно от
    role: и "warehouse", и "production" по факту не используют этот
    механизм для production (там доступ и так ограничен одним разделом),
    но поле физически применимо к любому не-админу."""
    if not _require_admin():
        return redirect(url_for("main.index"))

    user = User.query.get_or_404(user_id)
    mode = request.form.get("mode", "full")
    if mode == "full":
        user.allowed_sections = None
    else:
        selected = [code for code, _ in SECTIONS if request.form.get(f"section_{code}") == "on"]
        user.allowed_sections = ",".join(selected) if selected else "none"
    # Отдельная от режима доступа к разделам галочка — можно запретить
    # редактирование номенклатуры и при "полном доступе ко всем разделам"
    # (просмотр номенклатуры при этом остается).
    user.nomenclature_edit_allowed = request.form.get("nomenclature_edit") == "on"
    user.warehouse_mapping_allowed = request.form.get("warehouse_mapping") == "on"
    user.invoice_receiving_view_allowed = request.form.get("invoice_receiving_view") == "on"
    user.movement_view_allowed = request.form.get("movement_view") == "on"
    user.movement_complete_allowed = request.form.get("movement_complete") == "on"
    user.movement_receive_allowed = request.form.get("movement_receive") == "on"
    user.management_dashboard_allowed = request.form.get("management_dashboard") == "on"
    db.session.commit()
    flash(f"Доступ к разделам для «{user.username}» обновлен", "success")
    return redirect(url_for("auth.users"))


@bp.route("/users/<int:user_id>/warehouse", methods=["POST"])
@login_required
def update_warehouse(user_id):
    """Рабочий склад выбирает только администратор, в том числе для себя."""
    if not _require_admin():
        return redirect(url_for("main.index"))
    user = User.query.get_or_404(user_id)
    warehouse_id = request.form.get("warehouse_id", type=int)
    if warehouse_id and not Warehouse.query.filter_by(id=warehouse_id, is_active=True).first():
        flash("Выбранный склад не найден или отключен", "danger")
        return redirect(url_for("auth.users"))
    user.warehouse_id = warehouse_id
    db.session.commit()
    flash(f"Рабочий склад для «{user.username}» обновлен", "success")
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


@bp.route("/users/<int:user_id>/set-password", methods=["POST"])
@login_required
def set_password(user_id):
    """В отличие от reset_password (случайный временный пароль), здесь
    администратор задает пароль сам — например, чтобы сразу сообщить
    пользователю знакомый ему пароль."""
    if not _require_admin():
        return redirect(url_for("main.index"))

    user = User.query.get_or_404(user_id)
    new_password = request.form.get("password", "")
    if len(new_password) < 4:
        flash("Пароль слишком короткий (минимум 4 символа)", "danger")
        return redirect(url_for("auth.users"))

    user.set_password(new_password)
    db.session.commit()
    flash(f"Пароль для «{user.username}» изменен", "success")
    return redirect(url_for("auth.users"))


@bp.route("/users/<int:user_id>/username", methods=["POST"])
@login_required
def update_username(user_id):
    if not _require_admin():
        return redirect(url_for("main.index"))

    user = User.query.get_or_404(user_id)
    new_username = request.form.get("username", "").strip()
    if not new_username:
        flash("Укажите логин", "danger")
        return redirect(url_for("auth.users"))

    if User.query.filter(User.username == new_username, User.id != user.id).first():
        flash(f"Логин «{new_username}» уже занят", "danger")
        return redirect(url_for("auth.users"))

    old_username = user.username
    user.username = new_username
    db.session.commit()
    flash(f"Логин «{old_username}» изменен на «{new_username}»", "success")
    return redirect(url_for("auth.users"))


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
def delete_user(user_id):
    """Удаляет пользователя целиком (не путать с toggle_user — тем
    отключают вход, оставляя историю документов при авторе). В документах,
    где этот пользователь был автором, поле "Автор" после удаления просто
    станет пустым — сами документы никуда не деваются (created_by_id везде
    nullable и во всех шаблонах отображается через "{% if doc.created_by %}",
    без него не отображается)."""
    if not _require_admin():
        return redirect(url_for("main.index"))

    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("Нельзя удалить самого себя", "danger")
        return redirect(url_for("auth.users"))

    username = user.username
    db.session.delete(user)
    db.session.commit()
    flash(f"Пользователь «{username}» удален", "success")
    return redirect(url_for("auth.users"))


@bp.route("/users/<int:user_id>/revoke-sessions", methods=["POST"])
@login_required
def revoke_sessions(user_id):
    """Завершает ранее выданные сессии пользователя.

    Для самого администратора сохраняем текущий вход, поэтому кнопка на его
    строке действительно завершает сессии только на других устройствах.
    Для другого пользователя завершаются все его текущие входы.
    """
    if not _require_admin():
        return redirect(url_for("main.index"))

    user = User.query.get_or_404(user_id)
    user.session_version = (user.session_version or 0) + 1
    db.session.commit()

    if user.id == current_user.id:
        session["session_version"] = user.session_version
        flash("Сессии администратора на других устройствах завершены", "success")
    else:
        flash(f"Все активные сессии пользователя «{user.username}» завершены", "success")
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
