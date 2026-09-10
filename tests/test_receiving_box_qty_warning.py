"""Предупреждение при приемке в короб, если по одному виду товара в
коробе набралось больше порога (ProductCategory.box_qty_warning) —
например, кардиганов больше 25, шапок больше 300, свитеров больше 30.
Не блокирует прием, только предупреждает — вдруг лишний скан."""

from wms.extensions import db
from wms.models import Nomenclature, ProductCategory, Warehouse


def _make_doc_with_box(client_logged_in, warehouse):
    from wms.models import Box

    resp = client_logged_in.post(
        "/receiving/new", data={"warehouse_id": warehouse.id, "supplier": ""}, follow_redirects=True
    )
    doc_id = int(resp.request.path.rstrip("/").rsplit("/", 1)[-1])
    client_logged_in.post(f"/receiving/{doc_id}/boxes/create")
    box = Box.query.filter_by(warehouse_id=warehouse.id).order_by(Box.id.desc()).first()
    return doc_id, box.id


def test_scanning_over_threshold_returns_warning_in_json(db, client_logged_in):
    warehouse = Warehouse(code="WH-BQ1", name="Склад для проверки порога")
    db.session.add(warehouse)
    db.session.commit()
    category = ProductCategory(name="Кардиган-тест", keywords="кардиган-тест", box_qty_warning=25)
    db.session.add(category)
    db.session.commit()
    item = Nomenclature(
        sku="SKU-BQ1", barcode="7770000001", name="Кардиган тестовый", unit="шт", category_id=category.id
    )
    db.session.add(item)
    db.session.commit()

    doc_id, box_id = _make_doc_with_box(client_logged_in, warehouse)

    resp_ok = client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box_id}/lines/add-by-barcode",
        json={"barcode": item.barcode, "qty": 20},
    )
    assert resp_ok.get_json()["ok"] is True
    assert "warning" not in resp_ok.get_json()

    resp_warn = client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box_id}/lines/add-by-barcode",
        json={"barcode": item.barcode, "qty": 10},
    )
    data = resp_warn.get_json()
    assert data["ok"] is True
    assert data["warning"]["category"] == "Кардиган-тест"
    assert data["warning"]["qty"] == 30
    assert data["warning"]["threshold"] == 25


def test_manual_add_over_threshold_flashes_warning(db, client_logged_in):
    warehouse = Warehouse(code="WH-BQ2", name="Склад для проверки порога 2")
    db.session.add(warehouse)
    db.session.commit()
    category = ProductCategory(name="Шапка-тест", keywords="шапка-тест", box_qty_warning=300)
    db.session.add(category)
    db.session.commit()
    item = Nomenclature(
        sku="SKU-BQ2", barcode="7770000002", name="Шапка тестовая", unit="шт", category_id=category.id
    )
    db.session.add(item)
    db.session.commit()

    doc_id, box_id = _make_doc_with_box(client_logged_in, warehouse)

    resp = client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box_id}/lines/add",
        data={"nomenclature_id": item.id, "qty": 301},
        follow_redirects=True,
    )

    html = resp.get_data(as_text=True)
    assert "больше обычного порога" in html
    assert "Шапка-тест" in html


def test_no_warning_when_below_threshold(db, client_logged_in):
    warehouse = Warehouse(code="WH-BQ3", name="Склад для проверки порога 3")
    db.session.add(warehouse)
    db.session.commit()
    category = ProductCategory(name="Свитер-тест", keywords="свитер-тест", box_qty_warning=30)
    db.session.add(category)
    db.session.commit()
    item = Nomenclature(
        sku="SKU-BQ3", barcode="7770000003", name="Свитер тестовый", unit="шт", category_id=category.id
    )
    db.session.add(item)
    db.session.commit()

    doc_id, box_id = _make_doc_with_box(client_logged_in, warehouse)

    resp = client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box_id}/lines/add-by-barcode",
        json={"barcode": item.barcode, "qty": 30},
    )
    assert "warning" not in resp.get_json()


def test_no_warning_when_category_has_no_threshold(db, client_logged_in):
    warehouse = Warehouse(code="WH-BQ4", name="Склад для проверки порога 4")
    db.session.add(warehouse)
    db.session.commit()
    category = ProductCategory(name="Без порога", keywords="без-порога", box_qty_warning=None)
    db.session.add(category)
    db.session.commit()
    item = Nomenclature(
        sku="SKU-BQ4", barcode="7770000004", name="Товар без порога", unit="шт", category_id=category.id
    )
    db.session.add(item)
    db.session.commit()

    doc_id, box_id = _make_doc_with_box(client_logged_in, warehouse)

    resp = client_logged_in.post(
        f"/receiving/{doc_id}/boxes/{box_id}/lines/add-by-barcode",
        json={"barcode": item.barcode, "qty": 100000},
    )
    assert "warning" not in resp.get_json()


def test_bootstrap_categories_backfills_default_thresholds(db):
    """Приложение уже бутстрапит эти три вида при старте (см. app fixture) —
    симулируем склад, где они были созданы ДО появления порога (NULL), и
    проверяем, что повторный вызов bootstrap_categories() (как при каждом
    старте приложения) проставляет дефолтный порог, не трогая остальное."""
    from wms.utils.categorize import bootstrap_categories

    for name in ("Свитер", "Кардиган", "Шапка"):
        category = ProductCategory.query.filter_by(name=name).first()
        assert category is not None
        category.box_qty_warning = None
    db.session.commit()

    bootstrap_categories()

    assert ProductCategory.query.filter_by(name="Свитер").first().box_qty_warning == 30
    assert ProductCategory.query.filter_by(name="Кардиган").first().box_qty_warning == 25
    assert ProductCategory.query.filter_by(name="Шапка").first().box_qty_warning == 300


def test_bootstrap_categories_does_not_override_manually_set_threshold(db):
    from wms.utils.categorize import bootstrap_categories

    sweater = ProductCategory.query.filter_by(name="Свитер").first()
    sweater.box_qty_warning = 99
    db.session.commit()

    bootstrap_categories()

    sweater = ProductCategory.query.filter_by(name="Свитер").first()
    assert sweater.box_qty_warning == 99
