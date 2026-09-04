from sqlalchemy import text

from ..extensions import db

# Настройки серий номеров: ключ -> (префикс, ширина)
SERIES = {
    "warehouse": ("WH-", 3),
    "receiving": ("PRM-", 6),
    "placement": ("RAZ-", 6),
    "movement": ("PER-", 6),
    "box": ("BOX-", 6),
    "inventory": ("INV-", 6),
}


def next_number(key: str) -> str:
    """Атомарно увеличивает счетчик и возвращает отформатированный номер.

    Инкремент выполняется одним SQL-запросом (UPSERT) прямо в базе, а не
    read-modify-write в Python — это важно при одновременной работе
    нескольких пользователей: два запроса не могут получить один и тот же
    номер, даже если оба обратились к next_number почти одновременно.
    """
    prefix, width = SERIES[key]

    db.session.execute(
        text(
            "INSERT INTO counters (key, value) VALUES (:key, 1) "
            "ON CONFLICT(key) DO UPDATE SET value = value + 1"
        ),
        {"key": key},
    )
    value = db.session.execute(
        text("SELECT value FROM counters WHERE key = :key"), {"key": key}
    ).scalar_one()

    return f"{prefix}{value:0{width}d}"
