"""Журнал выработки сотрудников (см. models.WorkEvent): вызывается из мест,
где товар кладут в короб (pack) и где короб собирают на отгрузку (pick)."""

from datetime import datetime

from ..extensions import db
from ..models import WorkEvent
from .timezone import to_moscow


def work_day(moment=None):
    """Рабочий день по московскому времени."""
    return to_moscow(moment or datetime.utcnow()).date()


def _user_id(user):
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return user.id


def log_pack(user, box, nomenclature_id, qty):
    """Сотрудник положил qty единиц товара в короб. Не коммитит — событие
    уходит в ту же транзакцию, что и сама упаковка."""
    user_id = _user_id(user)
    if user_id is None or not qty or qty <= 0:
        return None
    event = WorkEvent(
        user_id=user_id, work_date=work_day(), kind="pack",
        box_id=box.id, nomenclature_id=nomenclature_id, qty=qty,
    )
    db.session.add(event)
    return event


def log_pick(user, box, document_id):
    """Сотрудник собрал короб на отгрузку (добавил в перемещение)."""
    user_id = _user_id(user)
    if user_id is None:
        return None
    event = WorkEvent(
        user_id=user_id, work_date=work_day(), kind="pick",
        box_id=box.id, qty=box.total_qty() or 0, ref_kind="movement", ref_id=document_id,
    )
    db.session.add(event)
    return event


def drop_pick_events(document_id, box_id=None):
    """Короб убрали из перемещения (или удалили документ) — собранным он
    больше не считается."""
    query = WorkEvent.query.filter_by(kind="pick", ref_kind="movement", ref_id=document_id)
    if box_id is not None:
        query = query.filter_by(box_id=box_id)
    query.delete()
