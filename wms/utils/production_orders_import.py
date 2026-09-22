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

# Необязательные колонки с датой входа в этап — если они есть в таблице,
# дата берется из них напрямую (надежный случай, см. ProductionOrder).
_STAGE_DATE_CANDIDATES = {
    "order_placed": ("дата заказа", "дата размещения заказа"),
    "workshop_search": ("дата поиска цеха", "цех найден", "дата цеха"),
    "sample_sewing": ("дата отшива образца", "образец отшит", "дата пошива образца"),
    "sample_approval": ("дата согласования", "образец согласован", "дата утверждения образца"),
    "batch_sewing": ("дата отшива партии", "партия в пошиве", "дата запуска партии"),
    "batch_ready": ("партия готова", "готово", "дата готовности", "передано на склад"),
}

# Ключевые слова в тексте статуса -> этап. Каждая фраза специально взята
# целиком (не голым словом вроде "образец" или "партия"), чтобы соседние по
# смыслу этапы не пересекались — например, и "отшив образца", и
# "согласование образца" содержат слово "образец", но не пересекаются как
# ФРАЗЫ, поэтому порядок проверки на результат не влияет.
_STAGE_STATUS_KEYWORDS = {
    "order_placed": ("заказ размещен", "заказ отправлен", "размещен заказ", "заказ на продукцию", "новый заказ"),
    "workshop_search": ("поиск цеха", "подбор цеха", "ищем цех", "в поиске цеха", "цех не найден"),
    "sample_sewing": ("отшив образца", "пошив образца", "образец в пошиве", "шьем образец", "пилотный образец"),
    "sample_approval": ("согласование образца", "образец согласован", "на согласовании", "согласован", "утвержд", "правки"),
    "batch_sewing": ("отшив партии", "пошив партии", "партия в производстве", "партия в пошиве", "производство партии"),
    "batch_ready": ("партия готова", "готова к отгрузке", "готово к отгрузке", "передано на склад", "передан на склад"),
}


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


def match_stage(raw_status):
    """Текст статуса из таблицы -> ключ этапа (PRODUCTION_ORDER_STAGE_KEYS)
    или None, если не распознан — тогда raw_status все равно сохраняется
    как есть, чтобы было видно, что именно не сопоставилось (см.
    production_orders.pending_diagnostics)."""
    text = _norm(raw_status).casefold()
    if not text:
        return None
    for stage in PRODUCTION_ORDER_STAGE_KEYS:
        keywords = _STAGE_STATUS_KEYWORDS.get(stage, ())
        if any(keyword in text for keyword in keywords):
            return stage
    return None


def map_columns(headers):
    """Возвращает structура с найденными колонками — используется и для
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
        "stage_dates": stage_date_columns,
    }
