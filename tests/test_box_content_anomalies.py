"""Отчет «Аномалии в коробах» (reports.box_anomalies_report) — сравнивает
кол-во товара в коробе с медианой по группе "вид товара (категория) +
размер" по всем цветам/SKU этой группы (один и тот же фасон того же
размера обычно упаковывают в короб одинаковым количеством независимо от
цвета) — сильное отклонение чаще всего означает ошибку приемки."""

from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, ProductCategory, Warehouse


def _make_warehouse(code):
    wh = Warehouse(code=code, name=f"Склад {code}")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(sku, barcode, name, category, size):
    item = Nomenclature(sku=sku, barcode=barcode, name=name, size=size, category=category, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _table_html(html):
    """Только видимая таблица отчета — без JSON-данных для модалки "короба
    с этим товаром" (см. reports.box_anomalies_report), которые намеренно
    содержат ВСЕ короба товара, включая неаномальные (см.
    test_anomaly_row_lists_all_boxes_with_that_nomenclature) — иначе номер
    неаномального короба того же товара ложно "находился" бы в html."""
    return html.split('<script type="application/json" id="anomalyBoxesData">')[0]


def _pack(warehouse, box_number, item, qty):
    box = Box(box_number=box_number, warehouse_id=warehouse.id, status="stored")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
    db.session.commit()
    return box


def test_flags_anomaly_even_with_only_two_boxes_in_group(db, client_logged_in):
    """Самый частый в жизни случай: в группе категория+размер всего два
    короба — по одному на каждый цвет. Если считать медиану по группе
    целиком (включая сам сравниваемый короб), выброс сдвигает медиану к
    себе и почти не отличается от нее по отношению — репорт эту явную
    ошибку приемки (кол-во руками поменяли в 10 раз) не покажет.
    Медиана должна считаться БЕЗ самого сравниваемого короба (см.
    reports._box_anomaly_rows)."""
    wh = _make_warehouse("WH-BA-6")
    category = ProductCategory(name="Кардиган-BA6", keywords="кардиган-ba6")
    db.session.add(category)
    db.session.commit()
    red = _make_item("SKU-BA6-RED", "9991000010", "Кардиган красный", category, "52")
    blue = _make_item("SKU-BA6-BLUE", "9991000011", "Кардиган синий", category, "52")

    _pack(wh, "BOX-BA6-1", red, 10)
    anomaly_box = _pack(wh, "BOX-BA6-2", blue, 100)  # руками поправили в 10 раз

    html = client_logged_in.get("/reports/box-anomalies").get_data(as_text=True)

    assert anomaly_box.box_number in html


def test_flags_box_far_above_group_median(db, client_logged_in):
    wh = _make_warehouse("WH-BA-1")
    category = ProductCategory(name="Кардиган-BA1", keywords="кардиган-ba1")
    db.session.add(category)
    db.session.commit()
    red = _make_item("SKU-BA-RED", "9991000001", "Кардиган красный", category, "44")
    blue = _make_item("SKU-BA-BLUE", "9991000002", "Кардиган синий", category, "44")

    # Типичное количество для этой связки категория+размер — 10, во всех
    # цветах, кроме одного короба с явно завышенным количеством.
    _pack(wh, "BOX-BA-1", red, 10)
    _pack(wh, "BOX-BA-2", blue, 10)
    _pack(wh, "BOX-BA-3", red, 11)
    anomaly_box = _pack(wh, "BOX-BA-4", blue, 40)

    html = client_logged_in.get("/reports/box-anomalies").get_data(as_text=True)

    assert anomaly_box.box_number in html
    table_html = _table_html(html)
    assert "BOX-BA-1" not in table_html
    assert "BOX-BA-2" not in table_html
    assert "BOX-BA-3" not in table_html


def test_flags_box_far_below_group_median(db, client_logged_in):
    wh = _make_warehouse("WH-BA-2")
    category = ProductCategory(name="Кардиган-BA2", keywords="кардиган-ba2")
    db.session.add(category)
    db.session.commit()
    red = _make_item("SKU-BA2-RED", "9991000003", "Кардиган красный", category, "46")
    blue = _make_item("SKU-BA2-BLUE", "9991000004", "Кардиган синий", category, "46")

    _pack(wh, "BOX-BA2-1", red, 12)
    _pack(wh, "BOX-BA2-2", blue, 12)
    anomaly_box = _pack(wh, "BOX-BA2-3", red, 1)

    html = client_logged_in.get("/reports/box-anomalies").get_data(as_text=True)

    assert anomaly_box.box_number in html
    table_html = _table_html(html)
    assert "BOX-BA2-1" not in table_html
    assert "BOX-BA2-2" not in table_html


def test_ignores_items_without_category_or_size(db, client_logged_in):
    wh = _make_warehouse("WH-BA-3")
    item = Nomenclature(
        sku="SKU-BA3-NOCAT", barcode="9991000005", name="Товар без категории", unit="шт"
    )
    db.session.add(item)
    db.session.commit()

    # Явно "аномальное" количество, но сравнивать не с чем — нет ни
    # категории, ни размера, а других коробов с этим же товаром тоже нет
    # (см. test_falls_back_to_same_sku_when_category_or_size_missing ниже
    # для случая, когда другие короба того же товара есть).
    box = _pack(wh, "BOX-BA3-1", item, 1000)

    html = client_logged_in.get("/reports/box-anomalies").get_data(as_text=True)

    assert box.box_number not in html


def test_falls_back_to_same_sku_when_category_or_size_missing(db, client_logged_in):
    """Без категории/размера сравнивать по группе "вид+размер" не с чем, но
    это не повод пропускать проверку целиком — если в системе есть другие
    короба с тем же самым SKU, сравниваем короб хотя бы с ними (в реальных
    данных это самый частый способ словить ошибку приемки: у товара просто
    не заполнена карточка, но одинаковых коробов этого SKU в системе много,
    и один из них резко выделяется по количеству)."""
    wh = _make_warehouse("WH-BA-7")
    item = Nomenclature(
        sku="SKU-BA7-NOCAT", barcode="9991000012", name="Товар без категории", unit="шт"
    )
    db.session.add(item)
    db.session.commit()

    for i in range(1, 6):
        _pack(wh, f"BOX-BA7-{i}", item, 10)
    anomaly_box = _pack(wh, "BOX-BA7-6", item, 100)

    html = client_logged_in.get("/reports/box-anomalies").get_data(as_text=True)

    assert anomaly_box.box_number in html
    assert "BOX-BA7-1" not in _table_html(html)


def test_warehouse_filter_still_uses_global_median(db, client_logged_in):
    """Медиана "типичного" количества считается по всем складам сразу — сам
    список в отчете можно сузить фильтром по складу, но это не должно
    портить сравнение (не сводить медиану к одному складу)."""
    wh_a = _make_warehouse("WH-BA-4A")
    wh_b = _make_warehouse("WH-BA-4B")
    category = ProductCategory(name="Кардиган-BA4", keywords="кардиган-ba4")
    db.session.add(category)
    db.session.commit()
    red = _make_item("SKU-BA4-RED", "9991000006", "Кардиган красный", category, "48")
    blue = _make_item("SKU-BA4-BLUE", "9991000007", "Кардиган синий", category, "48")

    # Эталонные короба — на другом складе.
    _pack(wh_a, "BOX-BA4-1", red, 10)
    _pack(wh_a, "BOX-BA4-2", blue, 10)
    anomaly_box = _pack(wh_b, "BOX-BA4-3", red, 45)

    html = client_logged_in.get(f"/reports/box-anomalies?warehouse_id={wh_b.id}").get_data(as_text=True)

    assert anomaly_box.box_number in html
    assert "BOX-BA4-1" not in _table_html(html)


def test_threshold_query_param_adjusts_sensitivity(db, client_logged_in):
    wh = _make_warehouse("WH-BA-5")
    category = ProductCategory(name="Кардиган-BA5", keywords="кардиган-ba5")
    db.session.add(category)
    db.session.commit()
    red = _make_item("SKU-BA5-RED", "9991000008", "Кардиган красный", category, "50")
    blue = _make_item("SKU-BA5-BLUE", "9991000009", "Кардиган синий", category, "50")

    _pack(wh, "BOX-BA5-1", red, 10)
    _pack(wh, "BOX-BA5-2", blue, 10)
    borderline_box = _pack(wh, "BOX-BA5-3", red, 18)  # x1.8 от медианы

    default_html = client_logged_in.get("/reports/box-anomalies").get_data(as_text=True)
    assert borderline_box.box_number not in default_html  # порог по умолчанию x3

    sensitive_html = client_logged_in.get(
        "/reports/box-anomalies?threshold=1.5"
    ).get_data(as_text=True)
    assert borderline_box.box_number in sensitive_html


def test_anomaly_row_lists_all_boxes_with_that_nomenclature(db, client_logged_in):
    """Клик по товару в аномальной строке должен показать ВСЕ короба с этим
    товаром (по всем складам, включая другой склад, вне зависимости от
    фильтра отчета) — чтобы свериться на месте, ошибка это или норма."""
    wh1 = _make_warehouse("WH-BA-7")
    wh2 = _make_warehouse("WH-BA-8")
    category = ProductCategory(name="Кардиган-BA7", keywords="кардиган-ba7")
    db.session.add(category)
    db.session.commit()
    red = _make_item("SKU-BA7-RED", "9991000012", "Кардиган красный", category, "48")
    blue = _make_item("SKU-BA7-BLUE", "9991000013", "Кардиган синий", category, "48")

    _pack(wh1, "BOX-BA7-1", red, 10)
    anomaly_box = _pack(wh1, "BOX-BA7-2", blue, 100)
    other_box = _pack(wh2, "BOX-BA7-3", blue, 12)

    html = client_logged_in.get("/reports/box-anomalies").get_data(as_text=True)

    assert anomaly_box.box_number in html
    assert 'data-nomenclature-id="{}"'.format(blue.id) in html
    # Данные для модалки — все короба этого товара, включая с другого
    # склада, зашиты в JSON на странице (см. anomalyBoxesData).
    assert other_box.box_number in html
    assert wh2.name in html
