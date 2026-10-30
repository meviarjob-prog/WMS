"""Поиск по каждой колонке списка приемок и галочка «Приемки без
накладных» (receiving.list_documents)."""

from datetime import datetime

from wms.extensions import db
from wms.models import (
    Nomenclature, ReceivingDocument, ReceivingLine, Supplier, SupplierReturn, User, Warehouse,
)


def _docs():
    wh1 = Warehouse(code="WH-CF1", name="Основной")
    wh2 = Warehouse(code="WH-CF2", name="Цех")
    sup = Supplier(name="ООО Ромашка", inn="1234567890")
    db.session.add_all([wh1, wh2, sup])
    db.session.commit()
    n = Nomenclature(sku="CF-1", barcode="CF0001", name="Шапка", unit="шт")
    db.session.add(n)
    db.session.commit()
    a = ReceivingDocument(number="CF-A", warehouse_id=wh1.id, supplier="Ручной поставщик",
                          order_number="ORD-77", status="draft", created_at=datetime(2026, 10, 5, 22, 30))
    b = ReceivingDocument(number="CF-B", warehouse_id=wh2.id, supplier_id=sup.id, supplier="ООО Ромашка",
                          status="completed", created_at=datetime(2026, 10, 7, 10, 0),
                          checked_in_1c_at=datetime(2026, 10, 8, 9, 0))
    db.session.add_all([a, b])
    db.session.commit()
    db.session.add_all([
        ReceivingLine(document_id=a.id, nomenclature_id=n.id, qty=10),
        ReceivingLine(document_id=b.id, nomenclature_id=n.id, qty=25),
        SupplierReturn(receiving_document_id=b.id, warehouse_id=wh2.id, nomenclature_id=n.id, qty=1),
    ])
    db.session.commit()
    return a, b


def _ids(client, query):
    html = client.get("/receiving/?" + query).get_data(as_text=True)
    return {name for name in ("CF-A", "CF-B") if name in html}


def test_no_invoice_checkbox_shows_only_manual_receivings(db, client_logged_in):
    _docs()
    assert _ids(client_logged_in, "no_invoice=on") == {"CF-A"}
    assert _ids(client_logged_in, "invoice_only=on") == {"CF-B"}
    html = client_logged_in.get("/receiving/").get_data(as_text=True)
    assert "Приемки без накладных" in html


def test_column_filters_number_warehouse_supplier_order(db, client_logged_in):
    _docs()
    assert _ids(client_logged_in, "f_number=CF-B") == {"CF-B"}
    assert _ids(client_logged_in, "f_warehouse=цех") == {"CF-B"}
    assert _ids(client_logged_in, "f_supplier=ромашка") == {"CF-B"}
    assert _ids(client_logged_in, "f_supplier=ручной") == {"CF-A"}
    assert _ids(client_logged_in, "f_order=ord-77") == {"CF-A"}
    assert _ids(client_logged_in, "f_number=CF&f_warehouse=Основной") == {"CF-A"}


def test_column_filters_status_qty_checked(db, client_logged_in):
    _docs()
    assert _ids(client_logged_in, "f_status=completed") == {"CF-B"}
    assert _ids(client_logged_in, "f_qty=10") == {"CF-A"}
    assert _ids(client_logged_in, "f_qty=25,0") == {"CF-B"}
    assert _ids(client_logged_in, "f_checked=yes") == {"CF-B"}
    assert _ids(client_logged_in, "f_checked=no") == {"CF-A"}
    assert _ids(client_logged_in, "f_returns=yes") == {"CF-B"}
    assert _ids(client_logged_in, "f_returns=no") == {"CF-A"}
    assert _ids(client_logged_in, "f_qty=abc") == {"CF-A", "CF-B"}  # мусор игнорируется


def test_column_filter_by_date_uses_moscow_day(db, client_logged_in):
    _docs()
    # CF-A создан 5.10 22:30 UTC = 6.10 01:30 МСК
    assert _ids(client_logged_in, "f_date=2026-10-06") == {"CF-A"}
    assert _ids(client_logged_in, "f_date=2026-10-05") == set()
    assert _ids(client_logged_in, "f_date=2026-10-07") == {"CF-B"}


def test_filter_row_rendered_and_reset_link(db, client_logged_in):
    _docs()
    html = client_logged_in.get("/receiving/?f_number=CF-A").get_data(as_text=True)
    assert 'name="f_number"' in html and 'value="CF-A"' in html
    assert "Сбросить" in html
