from datetime import date, datetime, timedelta
from uuid import uuid4

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import AppSetting, TradeCustomer, TradeOrder, TradeVisit, User
from ..utils.numbering import next_number

bp = Blueprint("trade", __name__)
SALES_STATUSES = ("submitted", "confirmed", "assembled", "delivered", "paid")


@bp.before_request
def require_trade_access():
    if current_user.is_production_only() or not current_user.has_section_access("trade"):
        abort(404)
    return None


def _month_bounds():
    today = date.today()
    start = today.replace(day=1)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return datetime.combine(start, datetime.min.time()), datetime.combine(
        next_month, datetime.min.time()
    )


def _money(value):
    try:
        return max(float(str(value or "0").replace(" ", "").replace(",", ".")), 0)
    except ValueError:
        return None


@bp.route("/")
def index():
    if not current_user.can_manage_trade():
        return redirect(url_for("trade.mobile"))

    start, end = _month_bounds()
    orders = TradeOrder.query.filter(
        TradeOrder.created_at >= start,
        TradeOrder.created_at < end,
        TradeOrder.status.in_(SALES_STATUSES),
    ).all()
    sales_total = sum(order.total_amount or 0 for order in orders)
    plan_setting = AppSetting.query.get("trade_monthly_sales_plan")
    monthly_plan = _money(plan_setting.value if plan_setting else 0) or 0
    plan_percent = round(sales_total / monthly_plan * 100, 1) if monthly_plan else None
    active_customers = TradeCustomer.query.filter_by(is_active=True).count()
    debt_total = (
        db.session.query(func.sum(TradeCustomer.debt))
        .filter(TradeCustomer.is_active.is_(True), TradeCustomer.debt > 0)
        .scalar()
        or 0
    )
    today = date.today()
    today_visits = TradeVisit.query.filter_by(visit_date=today).all()
    completed_today = sum(visit.status == "completed" for visit in today_visits)

    pipeline = []
    for status, label in (
        ("submitted", "Новые"),
        ("confirmed", "Подтверждены"),
        ("assembled", "Собраны"),
        ("delivered", "Доставлены"),
        ("paid", "Оплачены"),
    ):
        group = [order for order in orders if order.status == status]
        pipeline.append(
            {"status": status, "label": label, "count": len(group), "amount": sum(o.total_amount for o in group)}
        )

    rep_rows = (
        db.session.query(
            User,
            func.count(TradeOrder.id),
            func.coalesce(func.sum(TradeOrder.total_amount), 0),
        )
        .join(TradeOrder, TradeOrder.representative_id == User.id)
        .filter(
            TradeOrder.created_at >= start,
            TradeOrder.created_at < end,
            TradeOrder.status.in_(SALES_STATUSES),
        )
        .group_by(User.id)
        .order_by(func.sum(TradeOrder.total_amount).desc())
        .limit(6)
        .all()
    )
    recent_orders = TradeOrder.query.order_by(TradeOrder.created_at.desc()).limit(8).all()
    debtors = (
        TradeCustomer.query.filter(TradeCustomer.debt > 0, TradeCustomer.is_active.is_(True))
        .order_by(TradeCustomer.debt.desc())
        .limit(5)
        .all()
    )
    return render_template(
        "trade/dashboard.html",
        sales_total=sales_total,
        monthly_plan=monthly_plan,
        plan_percent=plan_percent,
        active_customers=active_customers,
        debt_total=debt_total,
        orders_count=len(orders),
        today_visits=len(today_visits),
        completed_today=completed_today,
        pipeline=pipeline,
        rep_rows=rep_rows,
        recent_orders=recent_orders,
        debtors=debtors,
    )


@bp.route("/mobile")
def mobile():
    representative = current_user
    if current_user.can_manage_trade() and request.args.get("rep_id", type=int):
        representative = User.query.get_or_404(request.args.get("rep_id", type=int))
    today = date.today()
    visits = (
        TradeVisit.query.filter_by(visit_date=today, representative_id=representative.id)
        .order_by(TradeVisit.sequence, TradeVisit.id)
        .all()
    )
    orders_today = TradeOrder.query.filter(
        TradeOrder.representative_id == representative.id,
        TradeOrder.created_at >= datetime.combine(today, datetime.min.time()),
        TradeOrder.created_at < datetime.combine(today + timedelta(days=1), datetime.min.time()),
        TradeOrder.status != "cancelled",
    ).all()
    return render_template(
        "trade/mobile.html",
        today=today,
        representative=representative,
        visits=visits,
        orders_today=orders_today,
        sales_today=sum(order.total_amount or 0 for order in orders_today),
        completed_count=sum(visit.status == "completed" for visit in visits),
    )


def _can_manage_visit(visit):
    return current_user.can_manage_trade() or visit.representative_id == current_user.id


@bp.route("/visits/<int:visit_id>/<action>", methods=["POST"])
def visit_action(visit_id, action):
    visit = TradeVisit.query.get_or_404(visit_id)
    if not _can_manage_visit(visit):
        abort(403)
    now = datetime.utcnow()
    if action == "start" and visit.status == "planned":
        visit.status = "in_progress"
        visit.started_at = now
        flash(f"Визит в «{visit.customer.name}» начат", "success")
    elif action == "complete" and visit.status in ("planned", "in_progress"):
        visit.status = "completed"
        visit.started_at = visit.started_at or now
        visit.completed_at = now
        visit.result = "visited"
        visit.customer.last_visit_at = now
        flash("Визит завершен", "success")
    elif action == "no-order" and visit.status in ("planned", "in_progress"):
        visit.status = "completed"
        visit.started_at = visit.started_at or now
        visit.completed_at = now
        visit.result = "no_order"
        visit.customer.last_visit_at = now
        flash("Визит завершен без заказа", "warning")
    else:
        flash("Это действие уже выполнено или недоступно", "warning")
        return redirect(url_for("trade.mobile"))
    db.session.commit()
    return redirect(url_for("trade.mobile"))


@bp.route("/customers/<int:customer_id>/order", methods=["GET", "POST"])
def new_order(customer_id):
    customer = TradeCustomer.query.get_or_404(customer_id)
    if not (current_user.is_admin or customer.assigned_rep_id == current_user.id):
        abort(403)
    visit = TradeVisit.query.filter_by(
        customer_id=customer.id,
        representative_id=current_user.id,
        visit_date=date.today(),
    ).first()
    if request.method == "POST":
        token = (request.form.get("submission_token") or "").strip()
        existing = TradeOrder.query.filter_by(submission_token=token).first() if token else None
        if existing:
            flash(f"Заказ {existing.number} уже был сохранен", "warning")
            return redirect(url_for("trade.mobile"))
        amount = _money(request.form.get("total_amount"))
        if not amount:
            flash("Укажите сумму заказа больше нуля", "danger")
        else:
            order = TradeOrder(
                number=next_number("trade_order", prefix="ZAK-", width=6),
                submission_token=token or uuid4().hex,
                customer_id=customer.id,
                representative_id=current_user.id,
                visit_id=visit.id if visit else None,
                status="submitted",
                total_amount=amount,
                comment=(request.form.get("comment") or "").strip() or None,
                submitted_at=datetime.utcnow(),
            )
            db.session.add(order)
            if visit:
                visit.status = "completed"
                visit.started_at = visit.started_at or datetime.utcnow()
                visit.completed_at = datetime.utcnow()
                visit.result = "order"
            customer.last_visit_at = datetime.utcnow()
            try:
                db.session.commit()
            except IntegrityError:
                # Два одинаковых запроса могут прийти почти одновременно:
                # предварительная проверка тогда еще не увидит первый.
                # Уникальный submission_token остается окончательной
                # защитой на уровне БД; второй запрос считаем успешным
                # повтором, а не показываем пользователю ошибку сервера.
                db.session.rollback()
                existing = TradeOrder.query.filter_by(submission_token=token).first()
                if existing:
                    flash(f"Заказ {existing.number} уже был сохранен", "warning")
                    return redirect(url_for("trade.mobile"))
                raise
            flash(f"Заказ {order.number} на {amount:,.0f} ₽ сохранен", "success")
            return redirect(url_for("trade.mobile"))
    return render_template(
        "trade/order_form.html", customer=customer, visit=visit, submission_token=uuid4().hex
    )


@bp.route("/setup", methods=["GET"])
def setup():
    if not current_user.can_manage_trade():
        abort(403)
    return render_template(
        "trade/setup.html",
        customers=TradeCustomer.query.order_by(TradeCustomer.name).all(),
        representatives=User.query.filter_by(role="trade_rep", is_active_user=True)
        .order_by(User.full_name, User.username)
        .all(),
    )


@bp.route("/setup/customer", methods=["POST"])
def create_customer():
    if not current_user.can_manage_trade():
        abort(403)
    name = (request.form.get("name") or "").strip()
    address = (request.form.get("address") or "").strip()
    representative_id = request.form.get("representative_id", type=int)
    if not name or not address:
        flash("Укажите название и адрес торговой точки", "danger")
    elif not User.query.filter_by(
        id=representative_id, role="trade_rep", is_active_user=True
    ).first():
        flash("Выберите торгового представителя", "danger")
    else:
        db.session.add(
            TradeCustomer(
                name=name,
                address=address,
                phone=(request.form.get("phone") or "").strip() or None,
                contact_name=(request.form.get("contact_name") or "").strip() or None,
                assigned_rep_id=representative_id,
                credit_limit=_money(request.form.get("credit_limit")) or 0,
            )
        )
        db.session.commit()
        flash("Торговая точка добавлена", "success")
    return redirect(url_for("trade.setup"))


@bp.route("/setup/visit", methods=["POST"])
def create_visit():
    if not current_user.can_manage_trade():
        abort(403)
    customer = TradeCustomer.query.get(request.form.get("customer_id", type=int))
    visit_date = request.form.get("visit_date")
    try:
        visit_date = datetime.strptime(visit_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        visit_date = None
    if not customer or not visit_date or not customer.assigned_rep_id:
        flash("Выберите точку с представителем и дату визита", "danger")
    elif TradeVisit.query.filter_by(
        customer_id=customer.id,
        representative_id=customer.assigned_rep_id,
        visit_date=visit_date,
    ).first():
        flash("Эта точка уже есть в маршруте на выбранную дату", "warning")
    else:
        max_sequence = (
            db.session.query(func.max(TradeVisit.sequence))
            .filter_by(visit_date=visit_date, representative_id=customer.assigned_rep_id)
            .scalar()
            or 0
        )
        db.session.add(
            TradeVisit(
                visit_date=visit_date,
                sequence=max_sequence + 1,
                customer_id=customer.id,
                representative_id=customer.assigned_rep_id,
            )
        )
        db.session.commit()
        flash("Точка добавлена в маршрут", "success")
    return redirect(url_for("trade.setup"))


def _require_trade_manager():
    if not current_user.can_manage_trade():
        abort(403)


@bp.route("/team")
def team():
    _require_trade_manager()
    members = (
        User.query.filter(User.role.in_(("trade_manager", "trade_rep")))
        .order_by(User.role, User.full_name, User.username)
        .all()
    )
    return render_template("trade/team.html", members=members)


@bp.route("/team/create", methods=["POST"])
def create_team_member():
    _require_trade_manager()
    username = (request.form.get("username") or "").strip()
    full_name = (request.form.get("full_name") or "").strip()
    password = request.form.get("password") or ""
    role = request.form.get("role") or "trade_rep"
    if role not in ("trade_manager", "trade_rep"):
        abort(400)
    if not username or not full_name:
        flash("Укажите логин и имя сотрудника", "danger")
    elif len(password) < 8:
        flash("Пароль должен содержать не менее 8 символов", "danger")
    elif User.query.filter_by(username=username).first():
        flash("Пользователь с таким логином уже существует", "danger")
    else:
        member = User(
            username=username,
            full_name=full_name,
            role=role,
            allowed_sections="trade",
            is_active_user=True,
        )
        member.set_password(password)
        db.session.add(member)
        db.session.commit()
        flash(f"Сотрудник «{full_name}» добавлен", "success")
    return redirect(url_for("trade.team"))


@bp.route("/team/<int:user_id>/password", methods=["POST"])
def set_team_member_password(user_id):
    _require_trade_manager()
    member = User.query.get_or_404(user_id)
    if member.role not in ("trade_manager", "trade_rep"):
        abort(404)
    password = request.form.get("password") or ""
    if len(password) < 8:
        flash("Пароль должен содержать не менее 8 символов", "danger")
    else:
        member.set_password(password)
        member.session_version = (member.session_version or 0) + 1
        db.session.commit()
        flash(f"Пароль для «{member.display_name()}» изменён", "success")
    return redirect(url_for("trade.team"))


@bp.route("/team/<int:user_id>/toggle", methods=["POST"])
def toggle_team_member(user_id):
    _require_trade_manager()
    member = User.query.get_or_404(user_id)
    if member.role not in ("trade_manager", "trade_rep"):
        abort(404)
    if member.id == current_user.id:
        flash("Нельзя отключить собственную учётную запись", "danger")
    else:
        member.is_active_user = not member.is_active_user
        if not member.is_active_user:
            member.session_version = (member.session_version or 0) + 1
        db.session.commit()
        flash(
            f"Сотрудник «{member.display_name()}» "
            f"{'включён' if member.is_active_user else 'отключён'}",
            "success",
        )
    return redirect(url_for("trade.team"))


@bp.route("/team/<int:user_id>/delete", methods=["POST"])
def delete_team_member(user_id):
    _require_trade_manager()
    member = User.query.get_or_404(user_id)
    if member.role not in ("trade_manager", "trade_rep"):
        abort(404)
    if member.id == current_user.id:
        flash("Нельзя удалить собственную учётную запись", "danger")
        return redirect(url_for("trade.team"))
    has_history = (
        TradeCustomer.query.filter_by(assigned_rep_id=member.id).first()
        or TradeVisit.query.filter_by(representative_id=member.id).first()
        or TradeOrder.query.filter_by(representative_id=member.id).first()
    )
    if has_history:
        member.is_active_user = False
        member.session_version = (member.session_version or 0) + 1
        db.session.commit()
        flash(
            "Сотрудник отключён, но сохранён в истории заказов и маршрутов",
            "warning",
        )
    else:
        name = member.display_name()
        db.session.delete(member)
        db.session.commit()
        flash(f"Сотрудник «{name}» удалён", "success")
    return redirect(url_for("trade.team"))
