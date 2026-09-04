"""Автоопределение вида товара (свитер/кардиган/шапка/...) по названию —
используется при создании и импорте номенклатуры, чтобы у товара сразу
была норма времени для расчета эффективности на производстве, даже если
её не задали вручную у конкретного артикула."""

from ..extensions import db
from ..models import ProductCategory

DEFAULT_CATEGORIES = [
    # (name, keywords, is_default)
    ("Свитер", "свитер,свитера,свитеры", False),
    ("Кардиган", "кардиган,кардиганы", False),
    ("Шапка", "шапка,шапки", True),
]


def bootstrap_categories():
    """Создает стартовый набор видов товара, если их еще нет вообще —
    не трогает уже существующие (админ мог их отредактировать/удалить)."""
    if ProductCategory.query.count() > 0:
        return

    for name, keywords, is_default in DEFAULT_CATEGORIES:
        db.session.add(
            ProductCategory(name=name, keywords=keywords, is_default=is_default)
        )
    db.session.commit()


def classify_by_name(name):
    """Ищет вид товара по вхождению одного из его ключевых слов в
    название (регистронезависимо). Если ничего не подошло — возвращает
    категорию-заглушку (is_default=True), если она есть."""
    if not name:
        return ProductCategory.query.filter_by(is_default=True).first()

    name_lower = name.lower()
    for category in ProductCategory.query.all():
        if not category.keywords:
            continue
        for keyword in category.keywords.split(","):
            keyword = keyword.strip().lower()
            if keyword and keyword in name_lower:
                return category

    return ProductCategory.query.filter_by(is_default=True).first()
