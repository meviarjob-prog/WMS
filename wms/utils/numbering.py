from ..extensions import db
from ..models import Counter

# Настройки серий номеров: ключ -> (префикс, ширина)
SERIES = {
    "receiving": ("PRM-", 6),
    "placement": ("RAZ-", 6),
    "movement": ("PER-", 6),
    "box": ("BOX-", 6),
}


def next_number(key: str) -> str:
    """Атомарно увеличивает счетчик и возвращает отформатированный номер."""
    prefix, width = SERIES[key]

    counter = Counter.query.filter_by(key=key).first()
    if counter is None:
        counter = Counter(key=key, value=0)
        db.session.add(counter)
        db.session.flush()

    counter.value += 1
    db.session.flush()

    return f"{prefix}{counter.value:0{width}d}"
