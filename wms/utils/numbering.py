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


def next_number(key: str, prefix: str = None, width: int = None) -> str:
    """Атомарно увеличивает счетчик и возвращает отформатированный номер.

    Инкремент выполняется одним SQL-запросом (UPSERT) прямо в базе, а не
    read-modify-write в Python — это важно при одновременной работе
    нескольких пользователей: два запроса не могут получить один и тот же
    номер, даже если оба обратились к next_number почти одновременно.

    prefix/width можно передать явно — для серий, которых нет в SERIES
    (например, отдельный счетчик ячеек на каждый ряд склада, ключ которого
    известен только в рантайме: "row_cells:<id ряда>").
    """
    if prefix is None or width is None:
        prefix, width = SERIES[key]

    db.session.execute(
        text(
            "INSERT INTO counters (key, value) VALUES (:key, 1) "
            # "counters." перед value — на Postgres голое "value" в SET
            # неоднозначно (может относиться и к строке таблицы, и к
            # вставляемой) и падает с "column reference is ambiguous";
            # SQLite такое молча разрешает как ссылку на строку таблицы,
            # поэтому баг был незаметен, пока не появился второй диалект.
            "ON CONFLICT(key) DO UPDATE SET value = counters.value + 1"
        ),
        {"key": key},
    )
    value = db.session.execute(
        text("SELECT value FROM counters WHERE key = :key"), {"key": key}
    ).scalar_one()

    return f"{prefix}{value:0{width}d}"
