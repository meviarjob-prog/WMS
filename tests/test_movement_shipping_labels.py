"""Стикеры отправления 58x40мм по выбранным перемещениям — по количеству
коробов в документе печатается столько же одинаковых стикеров (маршрут +
получатель + отправитель), без привязки к конкретному коробу — стикер не
несет ни номера, ни штрихкода короба (это отдельная от «Этикетки короба»
наклейка, см. warehouses.update_recipient для настройки получателя)."""

import re

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, MovementLine, Nomenclature, Warehouse
from wms.utils.shipping_label_pdf import build_movement_shipping_labels_pdf


def _make_document_with_boxes(n_boxes=2, suffix="1"):
    sender = Warehouse(code=f"WH-L{suffix}A", name="Склад-отправитель")
    dest = Warehouse(
        code=f"WH-L{suffix}B",
        name="ОЗОН: Казань",
        recipient_info="ООО Ромашка, ул. Тестовая 1, +7 900 000-00-00",
    )
    db.session.add_all([sender, dest])
    db.session.commit()

    item = Nomenclature(sku=f"SKU-L{suffix}", barcode=f"777000010{suffix}", name="Товар для стикера", unit="шт")
    db.session.add(item)
    db.session.commit()

    doc = MovementDocument(number=f"PER-L{suffix}", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()

    for i in range(n_boxes):
        box = Box(box_number=f"BOX-0009{suffix}{i}", warehouse_id=sender.id, status="open")
        db.session.add(box)
        db.session.commit()
        db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=3))
        db.session.add(
            MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=box.warehouse_id, from_cell_id=box.cell_id)
        )
        db.session.commit()

    return doc, dest


def test_export_shipping_labels_requires_selection(db, client_logged_in):
    resp = client_logged_in.get("/movement/shipping-labels.pdf", follow_redirects=True)
    assert "Выберите хотя бы одно перемещение" in resp.get_data(as_text=True)


def test_export_shipping_labels_returns_pdf(db, client_logged_in):
    doc, _dest = _make_document_with_boxes(n_boxes=2)

    resp = client_logged_in.get(f"/movement/shipping-labels.pdf?doc_ids={doc.id}")

    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF")


def _page_count(pdf_bytes):
    # Считаем маркеры начала страницы PDF (не "/Pages" — родительский узел
    # дерева страниц, который иначе тоже подошел бы под простой substring).
    return len(re.findall(rb"/Type\s*/Page(?!s)", pdf_bytes))


def test_build_shipping_labels_count_matches_box_count(db, client_logged_in):
    doc, _dest = _make_document_with_boxes(n_boxes=3)

    pdf_bytes = build_movement_shipping_labels_pdf([doc])

    assert _page_count(pdf_bytes) == 3


def test_build_shipping_labels_across_documents_sums_box_counts(db, client_logged_in):
    doc1, _dest1 = _make_document_with_boxes(n_boxes=2, suffix="1")
    doc2, _dest2 = _make_document_with_boxes(n_boxes=4, suffix="2")

    pdf_bytes = build_movement_shipping_labels_pdf([doc1, doc2])

    assert _page_count(pdf_bytes) == 6


def test_shipping_label_has_no_box_specific_barcode(db, client_logged_in):
    """Стикеры одного документа не привязаны к конкретному коробу — на них
    нет ни штрихкода, ни номера короба, поэтому в PDF не должно быть
    встроенного изображения (у обычной "Этикетки короба" оно всегда есть):
    никакого /XObject в ресурсах страницы (сам /Image — не показатель, это
    просто стандартная декларация возможностей ProcSet, которую reportlab
    пишет всегда, даже на чисто текстовую страницу)."""
    doc, _dest = _make_document_with_boxes(n_boxes=2)

    pdf_bytes = build_movement_shipping_labels_pdf([doc])

    assert b"/XObject" not in pdf_bytes


def test_warehouse_recipient_update(db, client_logged_in):
    wh = Warehouse(code="WH-L3", name="ОЗОН: Тверь")
    db.session.add(wh)
    db.session.commit()

    resp = client_logged_in.post(
        f"/warehouses/{wh.id}/recipient",
        data={"recipient_info": "ИП Иванов, г. Тверь, ул. Ленина 5"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    wh = Warehouse.query.get(wh.id)
    assert wh.recipient_info == "ИП Иванов, г. Тверь, ул. Ленина 5"


def test_warehouse_recipient_update_requires_admin(db, client):
    from wms.models import User

    staff = User(username="staffer-r", full_name="Складской", role="warehouse")
    staff.set_password("x")
    db.session.add(staff)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(staff.id)
        sess["_fresh"] = True

    wh = Warehouse(code="WH-L4", name="ОЗОН: Уфа")
    db.session.add(wh)
    db.session.commit()

    client.post(f"/warehouses/{wh.id}/recipient", data={"recipient_info": "Не должно сохраниться"})

    wh = Warehouse.query.get(wh.id)
    assert wh.recipient_info is None


def test_settings_page_shows_recipients_section(db, client_logged_in):
    """Настройка получателей для стикеров вынесена на страницу «Настройки»
    (админ) — там же, где управление пользователями, а не на «Склады и
    ячейки»."""
    wh = Warehouse(code="WH-L5", name="ОЗОН: Пермь", recipient_info="Тестовый получатель")
    db.session.add(wh)
    db.session.commit()

    resp = client_logged_in.get("/users")
    html = resp.get_data(as_text=True)

    assert "Стикеры перемещений" in html
    assert "Тестовый получатель" in html


def test_warehouses_page_no_longer_has_recipient_field(db, client_logged_in):
    wh = Warehouse(code="WH-L6", name="ОЗОН: Сочи", recipient_info="Не должно быть здесь")
    db.session.add(wh)
    db.session.commit()

    resp = client_logged_in.get("/warehouses/")
    html = resp.get_data(as_text=True)

    assert "Не должно быть здесь" not in html
