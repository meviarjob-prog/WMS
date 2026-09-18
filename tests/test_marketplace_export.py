"""Выгрузка состава коробов перемещения для маркетплейсов
(marketplace_export.py) — Ozon "Состав грузовых мест" и Wildberries "ШК
короба". Короб перемещения = одно грузовое место/короб маркетплейса,
штрихкоды которых сам маркетплейс генерирует в личном кабинете — их нужно
вставить в том же порядке, что и короба в перемещении."""

import io

import openpyxl

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, MovementLine, Nomenclature, OzonArticleMapping, Warehouse


def _make_ozon_movement():
    sender = Warehouse(code="WH-MPX-1", name="Склад-отправитель")
    dest = Warehouse(code="WH-MPX-2", name="ОЗОН: Тверь", marketplace="ozon", marketplace_city="Тверь")
    db.session.add_all([sender, dest])
    db.session.commit()

    item1 = Nomenclature(sku="SKU-MPX-1", barcode="8880100001", name="Товар 1", unit="шт")
    item2 = Nomenclature(sku="SKU-MPX-2", barcode="8880100002", name="Товар 2", unit="шт")
    db.session.add_all([item1, item2])
    db.session.commit()

    box1 = Box(box_number="BOX-MPX-1", warehouse_id=sender.id, status="open")
    box2 = Box(box_number="BOX-MPX-2", warehouse_id=sender.id, status="open")
    db.session.add_all([box1, box2])
    db.session.commit()
    db.session.add_all(
        [
            BoxItem(box_id=box1.id, nomenclature_id=item1.id, qty=5),
            BoxItem(box_id=box2.id, nomenclature_id=item2.id, qty=7),
        ]
    )

    doc = MovementDocument(
        number="PER-MPX-1", from_warehouse_id=sender.id, to_warehouse_id=dest.id, status="completed"
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add_all(
        [
            MovementLine(document_id=doc.id, box_id=box1.id, from_warehouse_id=sender.id),
            MovementLine(document_id=doc.id, box_id=box2.id, from_warehouse_id=sender.id),
        ]
    )
    db.session.commit()
    return doc, item1, item2


def _mapping_xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for barcode, article in rows:
        ws.append([barcode, article])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _read_xlsx_rows(data):
    wb = openpyxl.load_workbook(io.BytesIO(data))
    ws = wb.active
    return [row for row in ws.iter_rows(min_row=2, values_only=True) if row[0] is not None]


def test_ozon_mapping_upload_upserts_by_barcode(db, client_logged_in):
    resp = client_logged_in.post(
        "/marketplace-export/ozon-mapping",
        data={"file": (_mapping_xlsx([("8880100001", "Артикул-А"), ("8880100002", "Артикул-Б")]), "map.xlsx")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert OzonArticleMapping.query.count() == 2
    assert OzonArticleMapping.query.filter_by(barcode="8880100001").first().article == "Артикул-А"

    # Повторная загрузка того же штрихкода — обновляет, а не дублирует.
    client_logged_in.post(
        "/marketplace-export/ozon-mapping",
        data={"file": (_mapping_xlsx([("8880100001", "Артикул-В")]), "map2.xlsx")},
        content_type="multipart/form-data",
    )
    assert OzonArticleMapping.query.count() == 2
    assert OzonArticleMapping.query.filter_by(barcode="8880100001").first().article == "Артикул-В"


def test_ozon_mapping_upload_skips_rows_where_first_column_is_not_a_barcode(db, client_logged_in):
    """Реальный баг: админ по ошибке загрузил в сопоставление готовую
    "Заявку на поставку" (артикул/имя/количество) вместо исходного файла
    штрихкод-артикул от Ozon — первая колонка там текстовый артикул, а не
    штрихкод. Без проверки формата это создавало мусорные записи
    (barcode=текст артикула), которые никогда не совпадут с реальным
    Nomenclature.barcode, и "Заявка на поставку" молча не находила
    артикулы. Теперь такие строки пропускаются, а не превращаются в
    бесполезную запись."""
    resp = client_logged_in.post(
        "/marketplace-export/ozon-mapping",
        data={
            "file": (
                _mapping_xlsx(
                    [
                        ("артикул", "имя (необязательно)"),
                        ("Кардиган_Валерия_мол TM LIMITED A / Кардиганы (50-52)", 47),
                        ("8880100001", "Артикул-А"),
                    ]
                ),
                "map.xlsx",
            )
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert OzonArticleMapping.query.count() == 1
    assert OzonArticleMapping.query.first().barcode == "8880100001"
    assert "не похожа на штрихкод" in resp.get_data(as_text=True)


def test_ozon_mapping_upload_requires_admin(db, client):
    from wms.models import User

    worker = User(username="mpx-worker", role="warehouse")
    worker.set_password("x")
    db.session.add(worker)
    db.session.commit()
    with client.session_transaction() as session:
        session["_user_id"] = str(worker.id)
        session["_fresh"] = True

    resp = client.post(
        "/marketplace-export/ozon-mapping",
        data={"file": (_mapping_xlsx([("123", "Артикул")]), "map.xlsx")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert OzonArticleMapping.query.count() == 0


def test_ozon_export_builds_file_with_mapped_articles(db, client_logged_in):
    doc, item1, item2 = _make_ozon_movement()
    db.session.add_all(
        [
            OzonArticleMapping(barcode=item1.barcode, article="Артикул-1"),
            OzonArticleMapping(barcode=item2.barcode, article="Артикул-2"),
        ]
    )
    db.session.commit()

    resp = client_logged_in.post(
        f"/marketplace-export/movement/{doc.id}/ozon",
        data={"cargo_barcodes": "GM-0001\nGM-0002"},
    )

    assert resp.status_code == 200
    assert resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    rows = _read_xlsx_rows(resp.data)
    assert rows == [
        (item1.barcode, "Артикул-1", 5.0, None, None, "GM-0001", "Коробка"),
        (item2.barcode, "Артикул-2", 7.0, None, None, "GM-0002", "Коробка"),
    ]


def test_ozon_export_adds_second_sheet_with_box_to_gm_mapping(db, client_logged_in):
    """Второй лист файла — наше собственное сопоставление короба и
    грузоместа Ozon (не часть официального шаблона), для контроля при
    сборке/сверке поставки: наш короб, грузоместо Ozon, номенклатура,
    кол-во."""
    doc, item1, item2 = _make_ozon_movement()

    resp = client_logged_in.post(
        f"/marketplace-export/movement/{doc.id}/ozon",
        data={"cargo_barcodes": "GM-0001\nGM-0002"},
    )

    wb = openpyxl.load_workbook(io.BytesIO(resp.data))
    assert wb.sheetnames == ["Состав ГМ поставки", "Наши короба и ГМ"]

    ws2 = wb["Наши короба и ГМ"]
    header = [cell.value for cell in ws2[1]]
    assert header == ["Наш короб", "Грузоместо Ozon", "Номенклатура", "Кол-во"]

    rows = [row for row in ws2.iter_rows(min_row=2, values_only=True) if row[0] is not None]
    assert rows == [
        ("BOX-MPX-1", "GM-0001", item1.name, 5.0),
        ("BOX-MPX-2", "GM-0002", item2.name, 7.0),
    ]


def test_ozon_export_leaves_article_blank_when_not_mapped(db, client_logged_in):
    doc, item1, item2 = _make_ozon_movement()

    resp = client_logged_in.post(
        f"/marketplace-export/movement/{doc.id}/ozon",
        data={"cargo_barcodes": "GM-0001\nGM-0002"},
        follow_redirects=True,
    )

    rows = _read_xlsx_rows(resp.data)
    # Пустая строка при записи в xlsx читается обратно как None — это
    # особенность openpyxl/формата, а не отсутствие данных.
    assert not rows[0][1]
    assert not rows[1][1]


def test_ozon_supply_request_downloads_immediately_without_gm_barcodes(db, client_logged_in):
    """В отличие от ozon_package_composition, заявке на поставку не нужны
    штрихкоды ГМ (это не про короба, а про итоговое количество по SKU) —
    файл скачивается сразу по GET, без промежуточной формы."""
    doc, item1, item2 = _make_ozon_movement()
    db.session.add_all(
        [
            OzonArticleMapping(barcode=item1.barcode, article="Артикул-1"),
            OzonArticleMapping(barcode=item2.barcode, article="Артикул-2"),
        ]
    )
    db.session.commit()

    resp = client_logged_in.get(f"/marketplace-export/movement/{doc.id}/ozon/supply-request")

    assert resp.status_code == 200
    assert resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    rows = _read_xlsx_rows(resp.data)
    assert set(rows) == {("Артикул-1", item1.name, 5.0), ("Артикул-2", item2.name, 7.0)}


def test_ozon_supply_request_sums_quantity_across_boxes_of_same_sku(db, client_logged_in):
    doc, item1, _item2 = _make_ozon_movement()
    # Сопоставление артикула нужно, чтобы строка не отфильтровалась
    # _read_xlsx_rows (она отбрасывает строки с пустой первой колонкой —
    # для остальных шаблонов там всегда штрихкод, а тут — необязательный
    # артикул, который в этом тесте не важен, важна только сумма).
    db.session.add(OzonArticleMapping(barcode=item1.barcode, article="Артикул-1"))
    sender = Warehouse.query.filter_by(code="WH-MPX-1").first()
    extra_box = Box(box_number="BOX-MPX-3", warehouse_id=sender.id, status="open")
    db.session.add(extra_box)
    db.session.commit()
    db.session.add(BoxItem(box_id=extra_box.id, nomenclature_id=item1.id, qty=2))
    db.session.add(MovementLine(document_id=doc.id, box_id=extra_box.id, from_warehouse_id=sender.id))
    db.session.commit()

    resp = client_logged_in.get(f"/marketplace-export/movement/{doc.id}/ozon/supply-request")

    rows = _read_xlsx_rows(resp.data)
    row_for_item1 = next(r for r in rows if r[1] == item1.name)
    assert row_for_item1[2] == 7.0  # 5 (BOX-MPX-1) + 2 (BOX-MPX-3)


def test_ozon_export_rejects_mismatched_barcode_count(db, client_logged_in):
    doc, _item1, _item2 = _make_ozon_movement()

    resp = client_logged_in.post(
        f"/marketplace-export/movement/{doc.id}/ozon",
        data={"cargo_barcodes": "GM-0001"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    assert "ровно 2" in resp.get_data(as_text=True)


def test_wb_export_uses_our_own_box_numbers_as_shk_koroba(db, client_logged_in):
    """Для WB в "ШК короба" вносится наш собственный номер короба
    (Box.box_number) — WB не выдает отдельных штрихкодов, как Ozon, поэтому
    запрашивать какой-либо список не нужно, файл скачивается сразу по GET."""
    doc, item1, item2 = _make_ozon_movement()

    resp = client_logged_in.get(f"/marketplace-export/movement/{doc.id}/wb")

    assert resp.status_code == 200
    rows = _read_xlsx_rows(resp.data)
    assert rows == [
        (item1.barcode, 5.0, "BOX-MPX-1", None, None),
        (item2.barcode, 7.0, "BOX-MPX-2", None, None),
    ]


def test_wb_supply_request_has_two_columns_barcode_and_qty(db, client_logged_in):
    """Второй, упрощенный файл для WB (см. чат) — всего два столбца,
    суммарное количество по ВСЕМ коробам перемещения сразу, без разбивки
    по коробам (в отличие от wb_package_composition)."""
    doc, item1, item2 = _make_ozon_movement()

    resp = client_logged_in.get(f"/marketplace-export/movement/{doc.id}/wb/supply-request")

    assert resp.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(resp.data))
    ws = wb.active
    header = [cell.value for cell in ws[1]]
    assert header == ["Баркод", "Количество"]

    rows = _read_xlsx_rows(resp.data)
    assert rows == sorted([(item1.barcode, 5.0), (item2.barcode, 7.0)])


def test_wb_supply_request_sums_quantity_across_boxes_of_same_barcode(db, client_logged_in):
    doc, item1, _item2 = _make_ozon_movement()
    box3 = Box(box_number="BOX-MPX-3", warehouse_id=doc.from_warehouse_id, status="open")
    db.session.add(box3)
    db.session.commit()
    db.session.add(BoxItem(box_id=box3.id, nomenclature_id=item1.id, qty=3))
    db.session.add(MovementLine(document_id=doc.id, box_id=box3.id, from_warehouse_id=doc.from_warehouse_id))
    db.session.commit()

    resp = client_logged_in.get(f"/marketplace-export/movement/{doc.id}/wb/supply-request")

    rows = _read_xlsx_rows(resp.data)
    total_for_item1 = next(r for r in rows if r[0] == item1.barcode)
    assert total_for_item1[1] == 8.0
