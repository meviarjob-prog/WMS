from datetime import timedelta

# Все даты в БД хранятся в UTC (datetime.utcnow() по всей модели) — сервер
# не обязательно стоит в московском поясе. Для отображения в интерфейсе
# сдвигаем на московское время. У России нет перехода на летнее/зимнее
# время с 2014 года — фиксированный UTC+3, без DST и без учета региона.
MOSCOW_OFFSET = timedelta(hours=3)


def to_moscow(value):
    """UTC datetime -> московское время для отображения. None проходит
    насквозь (шаблоны и так часто пишут `if doc.created_at`)."""
    if value is None:
        return None
    return value + MOSCOW_OFFSET
