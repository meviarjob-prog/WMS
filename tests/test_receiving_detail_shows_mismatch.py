"""Регрессия: в списке приемок документы с расхождением (пересчет не
совпал с накладной) подсвечены (см. receiving.list_documents,
mismatch_doc_ids), но при открытии документа ("Открыть" -> receiving.detail)
расхождение по строкам нигде не было видно в десктопном варианте — колонка
"По накладной" показывалась только в мобильной сверке (confirm_invoice.html).
Теперь detail.html тоже показывает ожидаемое количество и подсвечивает
несовпадающие строки, тем же способом (⚠ и разница), что и сверка."""

from wms.extensions import db
from wms.models import Nomenclature, ReceivingDocument, ReceivingLine, Warehouse


def _make_warehouse(suffix):
    wh = Warehouse(code=f"WH-MISM{suffix}", name="Склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(suffix):
    item = Nomenclature(sku=f"SKU-MISM{suffix}", barcode=f"77709100{suffix}", name="Товар с расхождением", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def test_detail_desktop_table_highlights_mismatched_line(db, client_logged_in):
    wh = _make_warehouse("1")
    item = _make_item("1")
    doc = ReceivingDocument(number="MISM-0001", warehouse_id=wh.id, invoice_file_name="накладная.xlsx")
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=7, expected_qty=10))
    db.session.commit()

    html = client_logged_in.get(f"/receiving/{doc.id}").get_data(as_text=True)

    assert "По накладной" in html
    assert "text-danger fw-semibold" in html
    assert "-3" in html


def test_detail_desktop_table_does_not_highlight_matching_line(db, client_logged_in):
    wh = _make_warehouse("2")
    item = _make_item("2")
    doc = ReceivingDocument(number="MISM-0002", warehouse_id=wh.id, invoice_file_name="накладная.xlsx")
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10, expected_qty=10))
    db.session.commit()

    html = client_logged_in.get(f"/receiving/{doc.id}").get_data(as_text=True)

    assert "text-danger fw-semibold" not in html


def test_detail_manual_line_without_expected_qty_is_not_flagged_as_mismatch(db, client_logged_in):
    """Ручная приемка (не из накладной) — сравнивать не с чем, expected_qty
    пуст, строка не должна подсвечиваться как расхождение."""
    wh = _make_warehouse("3")
    item = _make_item("3")
    doc = ReceivingDocument(number="MISM-0003", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=5))
    db.session.commit()

    html = client_logged_in.get(f"/receiving/{doc.id}").get_data(as_text=True)

    assert "text-danger fw-semibold" not in html
