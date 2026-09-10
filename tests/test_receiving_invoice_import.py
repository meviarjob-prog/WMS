"""Загрузка приходной накладной из 1С (см. utils/receiving_invoice_import.py
и receiving.import_invoice_form/confirm_invoice/confirm_line): номер
накладной становится номером приемки, поставщик заводится в справочник
автоматически, товары/количество подставляются из файла, а кладовщик
сверяет их на упрощенной мобильной форме (список + количество + галочка)."""

import io

import openpyxl
import pytest

from wms.extensions import db
from wms.models import Nomenclature, ReceivingDocument, ReceivingLine, Supplier, UnplacedStock, Warehouse
from wms.utils.receiving_invoice_import import InvoiceParseError, parse_invoice


def _build_invoice_xlsx(
    number="1706",
    supplier_line="ИП Кииков Мурат Борисович, ИНН 091701566682, тел.: 89283882790",
    rows=(("НФ-00003575", "Кардиган бежевый MEVIAR Kids / Шапки (50-54)", 80),),
):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([f"Приходная накладная № {number} от 10 сентября 2026 г."])
    ws.append(["(Поступление от поставщика) Ш-009  СКЛАД2 ШОССЕЙНАЯ167"])
    ws.append([])
    ws.append([f"Поставщик:   {supplier_line}"])
    ws.append(["Контактное лицо:"])
    ws.append([])
    ws.append(["Покупатель:", 'Общество с ограниченной ответственностью "ТОР-ФАРМ"'])
    ws.append([])
    ws.append(["№", "Код", "Товары", "Мест", "Количество", "Цена", "Сумма"])
    for i, (code, name, qty) in enumerate(rows, start=1):
        ws.append([i, code, name, None, qty, 220.0, qty * 220.0])
    ws.append([])
    ws.append(["Итого:", "", "", "", "", "", sum(q for _, _, q in rows) * 220.0])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def test_parse_invoice_extracts_header_and_rows():
    file_stream = _build_invoice_xlsx()

    invoice = parse_invoice(file_stream)

    assert invoice.invoice_number == "1706"
    assert invoice.supplier_name.startswith("ИП Кииков Мурат Борисович")
    assert invoice.supplier_inn == "091701566682"
    assert invoice.supplier_phone == "89283882790"
    assert len(invoice.rows) == 1
    assert invoice.rows[0]["code"] == "НФ-00003575"
    assert invoice.rows[0]["qty"] == 80


def test_parse_invoice_multiple_rows_stops_at_itogo():
    file_stream = _build_invoice_xlsx(
        rows=(
            ("НФ-00003575", "Товар 1", 80),
            ("НФ-00003576", "Товар 2", 300),
        )
    )

    invoice = parse_invoice(file_stream)

    assert len(invoice.rows) == 2
    assert invoice.rows[1]["qty"] == 300


def test_parse_invoice_raises_on_unrecognized_file():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Просто случайный файл", "без нужной структуры"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    with pytest.raises(InvoiceParseError):
        parse_invoice(buf)


def _make_warehouse():
    wh = Warehouse(code="WH-INV1", name="Основной склад для накладных")
    db.session.add(wh)
    db.session.commit()
    return wh


def test_upload_creates_document_with_invoice_number_and_supplier(db, client_logged_in):
    warehouse = _make_warehouse()
    item = Nomenclature(sku="НФ-00003575", barcode="8880000001", name="Кардиган тест", unit="шт")
    db.session.add(item)
    db.session.commit()

    file_stream = _build_invoice_xlsx()
    resp = client_logged_in.post(
        "/receiving/import-invoice",
        data={"warehouse_id": warehouse.id, "file": (file_stream, "invoice.xlsx")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert resp.status_code == 200
    doc = ReceivingDocument.query.filter_by(number="1706").first()
    assert doc is not None
    assert doc.warehouse_id == warehouse.id
    assert doc.supplier_id is not None

    supplier = Supplier.query.get(doc.supplier_id)
    assert supplier.inn == "091701566682"

    line = doc.lines.first()
    assert line.nomenclature_id == item.id
    assert line.qty == 80
    assert line.expected_qty == 80
    assert line.confirmed is False

    # Редиректит сразу на мобильную форму сверки, а не на обычную детальную.
    assert resp.request.path == f"/receiving/{doc.id}/confirm"


def test_upload_reuses_existing_supplier_by_inn(db, client_logged_in):
    warehouse = _make_warehouse()
    existing = Supplier(name="Старое название", inn="091701566682", phone="000")
    db.session.add(existing)
    db.session.commit()
    item = Nomenclature(sku="НФ-00003575", barcode="8880000002", name="Кардиган тест 2", unit="шт")
    db.session.add(item)
    db.session.commit()

    client_logged_in.post(
        "/receiving/import-invoice",
        data={"warehouse_id": warehouse.id, "file": (_build_invoice_xlsx(), "invoice.xlsx")},
        content_type="multipart/form-data",
    )

    assert Supplier.query.filter_by(inn="091701566682").count() == 1
    doc = ReceivingDocument.query.filter_by(number="1706").first()
    assert doc.supplier_id == existing.id


def test_upload_rejects_duplicate_invoice_number(db, client_logged_in):
    warehouse = _make_warehouse()
    item = Nomenclature(sku="НФ-00003575", barcode="8880000003", name="Кардиган тест 3", unit="шт")
    db.session.add(item)
    db.session.commit()

    client_logged_in.post(
        "/receiving/import-invoice",
        data={"warehouse_id": warehouse.id, "file": (_build_invoice_xlsx(), "invoice.xlsx")},
        content_type="multipart/form-data",
    )
    resp = client_logged_in.post(
        "/receiving/import-invoice",
        data={"warehouse_id": warehouse.id, "file": (_build_invoice_xlsx(), "invoice.xlsx")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert "уже была загружена раньше" in resp.get_data(as_text=True)
    assert ReceivingDocument.query.filter_by(number="1706").count() == 1


def test_upload_reports_unmatched_codes_but_keeps_matched(db, client_logged_in):
    warehouse = _make_warehouse()
    item = Nomenclature(sku="НФ-00003575", barcode="8880000004", name="Кардиган тест 4", unit="шт")
    db.session.add(item)
    db.session.commit()

    file_stream = _build_invoice_xlsx(
        rows=(
            ("НФ-00003575", "Кардиган тест 4", 80),
            ("НФ-НЕИЗВЕСТНЫЙ", "Товар без совпадения", 5),
        )
    )
    resp = client_logged_in.post(
        "/receiving/import-invoice",
        data={"warehouse_id": warehouse.id, "file": (file_stream, "invoice.xlsx")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    html = resp.get_data(as_text=True)
    assert "Не найдено по коду" in html
    doc = ReceivingDocument.query.filter_by(number="1706").first()
    assert doc.lines.count() == 1


def test_confirm_line_updates_qty_and_confirmed_flag(db, client_logged_in):
    warehouse = _make_warehouse()
    item = Nomenclature(sku="НФ-00003575", barcode="8880000005", name="Кардиган тест 5", unit="шт")
    db.session.add(item)
    db.session.commit()
    client_logged_in.post(
        "/receiving/import-invoice",
        data={"warehouse_id": warehouse.id, "file": (_build_invoice_xlsx(), "invoice.xlsx")},
        content_type="multipart/form-data",
    )
    doc = ReceivingDocument.query.filter_by(number="1706").first()
    line = doc.lines.first()

    resp = client_logged_in.post(
        f"/receiving/{doc.id}/lines/{line.id}/confirm",
        json={"qty": 78, "confirmed": True},
    )

    data = resp.get_json()
    assert data["ok"] is True
    assert data["confirmed_count"] == 1
    assert data["total_count"] == 1

    line = ReceivingLine.query.get(line.id)
    assert line.qty == 78
    assert line.confirmed is True
    assert line.expected_qty == 80  # исходное количество из накладной не трогаем


def test_completing_confirmed_invoice_credits_unplaced_stock(db, client_logged_in):
    warehouse = _make_warehouse()
    item = Nomenclature(sku="НФ-00003575", barcode="8880000006", name="Кардиган тест 6", unit="шт")
    db.session.add(item)
    db.session.commit()
    client_logged_in.post(
        "/receiving/import-invoice",
        data={"warehouse_id": warehouse.id, "file": (_build_invoice_xlsx(), "invoice.xlsx")},
        content_type="multipart/form-data",
    )
    doc = ReceivingDocument.query.filter_by(number="1706").first()
    line = doc.lines.first()
    client_logged_in.post(f"/receiving/{doc.id}/lines/{line.id}/confirm", json={"qty": 79, "confirmed": True})

    client_logged_in.post(f"/receiving/{doc.id}/complete")

    stock = UnplacedStock.query.filter_by(warehouse_id=warehouse.id, nomenclature_id=item.id).first()
    assert stock is not None
    assert stock.qty == 79
