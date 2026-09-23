"""Сопоставление колонок Google-таблицы заказов на производство с полями
ProductionOrder — см. чат (панель руководителя) и production_orders.py.

Название листа/колонок заранее не фиксировано — таблицу ведет менеджер
вручную, а не система, поэтому здесь тот же принцип, что и в остальном
проекте при разборе внешних файлов (см. SyncWMS.bsl, shipment_plan_import.py):
подбираем колонку по списку вероятных подписей, а не по номеру/точному
названию, и не падаем на колонках, которых нет, а просто не заполняем
то, что из них бы взялось."""

from ..models import PRODUCTION_ORDER_STAGE_KEYS

_ORDER_NUMBER_CANDIDATES = ("№ заказа", "номер заказа", "заказ №", "заказ", "№")
_MARKETPLACE_CANDIDATES = ("маркетплейс", "мп", "площадка")
_STATUS_CANDIDATES = ("статус", "этап", "стадия", "текущий этап", "текущий статус")
# Дедлайн партии (одна дата на заказ, см. чат) — ориентир ПОСЛЕ внесения
# заказа в 1С, когда дальше уже нет отдельных статусов этой таблицы.
_DEADLINE_CANDIDATES = ("дедлайн", "дедлайн партии", "срок готовности", "срок партии", "срок")

# Необязательные колонки с датой входа в этап — если они есть в таблице,
# дата берется из них напрямую (надежный случай, см. ProductionOrder).
_STAGE_DATE_CANDIDATES = {
    "workshop_search": ("дата поиска цеха", "дата поиска поставщика", "цех найден", "дата цеха"),
    "sample_sewing": ("дата отшива образца", "образец отшит", "дата пошива образца"),
    "sample_approved": ("дата согласования", "дата согласования образца", "образец согласован"),
    "photo_requested": ("дата запроса фото", "фото запрошено", "дата фото"),
    "mp_card_created": ("дата карточки мп", "карточка заведена", "дата карточки"),
    "data_in_1c": ("дата данных в 1с", "занесено в 1с", "дата занесения в 1с"),
    "order_in_1c": ("дата заказа в 1с", "заказ внесен в 1с", "дата внесения заказа"),
}

# Ключевые слова в тексте статуса -> этап. Каждая фраза специально взята
# целиком (не голым словом вроде "образец" или "карточка"), чтобы соседние
# по смыслу этапы не пересекались — например, и "отшив образца", и
# "согласование образца" содержат слово "образец", но не пересекаются как
# ФРАЗЫ, поэтому порядок проверки на результат не влияет.
_STAGE_STATUS_KEYWORDS = {
    "workshop_search": (
        "поиск цеха", "поиск поставщика", "подбор цеха", "подбор поставщика",
        "ищем цех", "в поиске цеха", "цех не найден",
    ),
    "sample_sewing": (
        "отшив образца", "пошив образца", "образец в пошиве", "шьем образец", "пилотный образец",
    ),
    "sample_approved": (
        "образец согласован", "согласование образца завершено", "образец утвержден",
    ),
    "photo_requested": (
        "запрос фото", "запросили фото", "ждем фото образца", "фото образца запрошено",
    ),
    "mp_card_created": (
        "карточка на мп", "карточка мп заведена", "заведена карточка", "карточка товара заведена",
    ),
    "data_in_1c": (
        "данные в 1с", "занесено в 1с", "карточка в 1с", "заведено в 1с",
    ),
    "order_in_1c": (
        "заказ в 1с", "заказ внесен", "внесен заказ", "заказ занесен в 1с", "заказ добавлен в 1с",
    ),
}

# "Отмена" и "переделка" образца — статус-модификаторы, а не этапы воронки
# (см. ProductionOrder.sample_cancelled_at/rework_count) — проверяются
# ДО обычных этапов, поэтому не должны пересекаться с их фразами.
_CANCELLED_KEYWORDS = (
    "образец отменен", "образец отменён", "отмена образца", "заказ отменен", "заказ отменён", "отменен", "отменён",
)
_REWORK_KEYWORDS = (
    "переделка", "на переделке", "отправлен на переделку", "образец на переделке", "переделать образец",
)


def _norm(text):
    return str(text).strip() if text is not None else ""


def _norm_header(text):
    return _norm(text).casefold()


def find_column(headers, candidates):
    """Первая колонка из headers, чье название (без учета регистра/пробелов
    по краям) совпадает с одним из candidates, либо содержит его целиком
    как подстроку — на случай пояснений в скобках вроде "Статус (для WMS)"."""
    normalized = {_norm_header(h): h for h in headers}
    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate]
    for header in headers:
        header_norm = _norm_header(header)
        if any(candidate in header_norm for candidate in candidates):
            return header
    return None


def classify_status(raw_status):
    """Текст статуса из таблицы -> ("stage", ключ_этапа) |
    ("cancelled", None) | ("rework", None) | (None, None), если текст не
    распознан вообще — тогда raw_status все равно сохраняется как есть,
    чтобы было видно, что именно не сопоставилось (см. production_orders —
    диагностика на странице настроек)."""
    text = _norm(raw_status).casefold()
    if not text:
        return (None, None)
    if any(keyword in text for keyword in _CANCELLED_KEYWORDS):
        return ("cancelled", None)
    if any(keyword in text for keyword in _REWORK_KEYWORDS):
        return ("rework", None)
    for stage in PRODUCTION_ORDER_STAGE_KEYS:
        keywords = _STAGE_STATUS_KEYWORDS.get(stage, ())
        if any(keyword in text for keyword in keywords):
            return ("stage", stage)
    return (None, None)


def map_columns(headers):
    """Возвращает структуру с найденными колонками — используется и для
    самого импорта, и для диагностики на странице настроек (видно, что
    сопоставилось, а что нет, без необходимости заранее знать точные
    названия колонок реальной таблицы)."""
    stage_date_columns = {
        stage: find_column(headers, candidates)
        for stage, candidates in _STAGE_DATE_CANDIDATES.items()
    }
    return {
        "order_number": find_column(headers, _ORDER_NUMBER_CANDIDATES),
        "marketplace": find_column(headers, _MARKETPLACE_CANDIDATES),
        "status": find_column(headers, _STATUS_CANDIDATES),
        "deadline": find_column(headers, _DEADLINE_CANDIDATES),
        "stage_dates": stage_date_columns,
    }
