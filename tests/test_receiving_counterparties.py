"""Справочник контрагентов WMS: фактическое сопоставление Supplier <-> ИНН."""

from datetime import datetime

from wms.extensions import db as _db
from wms.models import ReceivingDocument, Supplier, Warehouse


def _warehouse():
    warehouse = Warehouse(code="WH-CP", name="Основной склад")
    _db.session.add(warehouse)
    _db.session.flush()
    return warehouse


def test_counterparties_show_inn_and_receiving_statistics(db, client_logged_in):
    warehouse = _warehouse()
    supplier = Supplier(
        name="ИП Проверяемый Поставщик",
        inn="091234567890",
        phone="+7 900 111-22-33",
    )
    db.session.add(supplier)
    db.session.flush()
    db.session.add_all(
        [
            ReceivingDocument(
                number="CP-1",
                warehouse_id=warehouse.id,
                supplier=supplier.name,
                supplier_id=supplier.id,
                created_at=datetime(2026, 11, 19, 10, 0),
            ),
            ReceivingDocument(
                number="CP-2",
                warehouse_id=warehouse.id,
                supplier=supplier.name,
                supplier_id=supplier.id,
                created_at=datetime(2026, 11, 20, 12, 30),
            ),
        ]
    )
    db.session.commit()

    response = client_logged_in.get("/receiving/counterparties")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "ИП Проверяемый Поставщик" in html
    assert "091234567890" in html
    assert "+7 900 111-22-33" in html
    assert ">2</td>" in html
    assert "20.11.2026 15:30" in html


def test_counterparties_searches_by_inn_and_marks_missing_inn(db, client_logged_in):
    db.session.add_all(
        [
            Supplier(name="ООО Альфа", inn="7701234567"),
            Supplier(name="ООО Без реквизитов", inn=None),
        ]
    )
    db.session.commit()

    response = client_logged_in.get("/receiving/counterparties?q=7701234567")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "ООО Альфа" in html
    assert "ООО Без реквизитов" not in html

    response = client_logged_in.get("/receiving/counterparties")
    assert "ИНН не указан" in response.get_data(as_text=True)


def test_counterparties_requires_receiving_section(db, client):
    from tests.test_nomenclature_edit_permission import _login_as, _make_staff_user

    user = _make_staff_user()
    user.allowed_sections = "nomenclature"
    db.session.commit()
    _login_as(client, user)

    response = client.get("/receiving/counterparties", follow_redirects=True)

    assert response.status_code == 200
    assert "Этот раздел вам не доступен" in response.get_data(as_text=True)
