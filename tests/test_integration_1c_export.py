"""Выгрузка для 1С (/integrations/1c/api/export) и подтверждение приема
(/integrations/1c/api/export/confirm) — доступ по токену, маршрутизация
склада назначения перемещений на фиктивный "Товары в пути на Фулфилмент"
(кроме прямых перемещений между двумя настоящими физическими складами),
фильтр по галочке бухгалтера "внесено в 1С" и группировка возвратов
поставщику по приемке (см. receiving.complete/разбраковка)."""

import json
from datetime import datetime

from wms.extensions import db
from wms.models import (
    AppSetting,
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    Nomenclature,
    ReceivingDocument,
    ReceivingLine,
    SupplierReturn,
    Warehouse,
)

TOKEN = "test-1c-token"


def _set_token():
    db.session.add(AppSetting(key="api_1c_token", value=TOKEN))
    db.session.commit()


def _make_item(barcode, name="Товар для 1С"):
    item = Nomenclature(sku=barcode, barcode=barcode, name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _ship_box(
    sender, receiver, item, qty, box_number, client, accounting_entered=False, mark_request_created=True
):
    box = Box(box_number=box_number, warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
    doc = MovementDocument(
        number=f"PER-{box_number}", from_warehouse_id=sender.id, to_warehouse_id=receiver.id
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id, from_cell_id=box.cell_id)
    )
    db.session.commit()
    client.post(f"/movement/{doc.id}/complete")
    doc = MovementDocument.query.get(doc.id)
    if mark_request_created:
        # Гейт выгрузки в 1С — "Создана заявка на МП" (marketplace_request_
        # created_at), а не "Отгружено" (см. чат) — до приемки на складе
        # получателя ждать не нужно.
        doc.marketplace_request_created_at = doc.completed_at
    if accounting_entered:
        from datetime import datetime

        doc.accounting_entered_at = datetime.utcnow()
    db.session.commit()
    return doc


def test_export_requires_token(db, client):
    resp = client.get("/integrations/1c/api/export")
    assert resp.status_code == 401


def test_reconciliation_requires_token(db, client):
    resp = client.get("/integrations/1c/api/reconciliation")
    assert resp.status_code == 401


def test_reconciliation_validates_and_applies_period(db, client_logged_in):
    _set_token()
    response = client_logged_in.get(
        "/integrations/1c/api/reconciliation?date_from=2026-10-01&date_to=2026-09-01",
        headers={"X-1C-Token": TOKEN},
    )
    assert response.status_code == 400

    response = client_logged_in.get(
        "/integrations/1c/api/reconciliation?date_from=2099-01-01&date_to=2099-01-31",
        headers={"X-1C-Token": TOKEN},
    )
    assert response.status_code == 200
    assert response.get_json()["movements"] == []
    assert response.get_json()["receivings"] == []


def test_reconciliation_returns_synced_movements_and_invoice_receivings(
    db, client_logged_in
):
    _set_token()
    sender = Warehouse(code="WH-REC-S", name="Основной склад")
    receiver = Warehouse(code="WH-REC-T", name="ОЗОН: Казань", marketplace="ozon")
    item = _make_item("460000000001", "Товар сверки")
    db.session.add_all([sender, receiver])
    db.session.commit()

    # Один SKU лежит в двух коробах — для 1С он должен уйти одной сводной
    # строкой, иначе объединенная строка документа даст ложное расхождение.
    first = _ship_box(sender, receiver, item, 4, "REC-A", client_logged_in)
    extra_box = Box(box_number="REC-B", warehouse_id=sender.id, status="open")
    db.session.add(extra_box)
    db.session.commit()
    db.session.add_all(
        [
            BoxItem(box_id=extra_box.id, nomenclature_id=item.id, qty=6),
            MovementLine(
                document_id=first.id,
                box_id=extra_box.id,
                from_warehouse_id=sender.id,
            ),
        ]
    )
    first.synced_to_1c_at = datetime.utcnow()

    receiving = ReceivingDocument(
        number="INV-RECONCILE",
        warehouse_id=sender.id,
        status="completed",
        invoice_file_name="invoice.xlsx",
        completed_at=datetime.utcnow(),
    )
    db.session.add(receiving)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=receiving.id, nomenclature_id=item.id, qty=7))
    db.session.commit()

    response = client_logged_in.get(
        "/integrations/1c/api/reconciliation", headers={"X-1C-Token": TOKEN}
    )
    assert response.status_code == 200
    payload = response.get_json()
    movement = next(row for row in payload["movements"] if row["id"] == first.id)
    assert movement["lines"] == [
        {"barcode": item.barcode, "name": item.name, "qty": 10.0}
    ]
    assert payload["receivings"] == [
        {
            "id": receiving.id,
            "number": receiving.number,
            "invoice_number": receiving.number,
            "lines": [{"barcode": item.barcode, "name": item.name, "qty": 7.0}],
        }
    ]


def test_export_movement_uses_fulfillment_warehouse_by_default(db, client_logged_in):
    _set_token()
    sender = Warehouse(code="WH-1C-1", name="Основной склад")
    city = Warehouse(code="WH-1C-2", name="ОЗОН: Казань", marketplace="ozon", marketplace_city="Казань")
    db.session.add_all([sender, city])
    db.session.commit()
    item = _make_item("9990000001")

    _ship_box(sender, city, item, 5, "BOX-1C-1", client_logged_in)

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()
    movement = next(m for m in data["movements"] if m["from_warehouse"] == "Основной склад")
    assert movement["to_warehouse"] == "Товары в пути на Фулфилмент"
    assert movement["lines"][0]["barcode"] == "9990000001"
    assert movement["lines"][0]["name"] == "Товар для 1С"


def test_export_direct_transfer_between_known_warehouses_keeps_real_name(db, client_logged_in):
    _set_token()
    main = Warehouse(code="WH-1C-3", name="Основной склад")
    second = Warehouse(code="WH-1C-4", name="Склад №2 (Шоссейная 167)")
    db.session.add_all([main, second])
    db.session.commit()
    item = _make_item("9990000002")

    _ship_box(main, second, item, 3, "BOX-1C-2", client_logged_in)

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()
    movement = next(m for m in data["movements"] if m["from_warehouse"] == "Основной склад")
    assert movement["to_warehouse"] == "Склад №2 (Шоссейная 167)"


def test_export_excludes_movements_marked_entered_in_1c(db, client_logged_in):
    _set_token()
    sender = Warehouse(code="WH-1C-5", name="Основной склад")
    city = Warehouse(code="WH-1C-6", name="ОЗОН: Пермь", marketplace="ozon", marketplace_city="Пермь")
    db.session.add_all([sender, city])
    db.session.commit()
    item = _make_item("9990000003")

    _ship_box(sender, city, item, 2, "BOX-1C-3", client_logged_in, accounting_entered=True)

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()
    assert all(m["number"] != "PER-BOX-1C-3" for m in data["movements"])


def test_export_excludes_movement_before_marketplace_request_created(db, client_logged_in):
    """Перемещение выгружается в 1С только когда дошло до статуса "Создана
    заявка" (marketplace_request_created_at заполнен, см. чат) — на "Собрано"
    выгружать еще рано, а ждать "Отгружено" (Принято на складе) не нужно."""
    _set_token()
    sender = Warehouse(code="WH-1C-9", name="Основной склад")
    city = Warehouse(code="WH-1C-10", name="ОЗОН: Уфа", marketplace="ozon", marketplace_city="Уфа")
    db.session.add_all([sender, city])
    db.session.commit()
    item = _make_item("9990000007")

    doc = _ship_box(sender, city, item, 4, "BOX-1C-4", client_logged_in, mark_request_created=False)
    assert doc.marketplace_request_created_at is None

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()
    assert all(m["number"] != "PER-BOX-1C-4" for m in data["movements"])


def test_export_comment_includes_marketplace_request_number(db, client_logged_in):
    """Номер заявки на приемку у маркетплейса (вносится вручную в списке
    перемещений, см. movement.update_marketplace_request_number) должен
    попадать в комментарий документа при выгрузке в 1С — рядом с номером
    перемещения, чтобы документ можно было найти в 1С по любому из них."""
    _set_token()
    sender = Warehouse(code="WH-1C-11", name="Основной склад")
    city = Warehouse(code="WH-1C-12", name="ОЗОН: Сочи", marketplace="ozon", marketplace_city="Сочи")
    db.session.add_all([sender, city])
    db.session.commit()
    item = _make_item("9990000008")

    doc = _ship_box(sender, city, item, 1, "BOX-1C-5", client_logged_in)
    doc.marketplace_request_number = "REQ-778"
    db.session.commit()

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()
    movement = next(m for m in data["movements"] if m["number"] == "PER-BOX-1C-5")
    assert "(№ заявки МП: REQ-778)" in movement["comment"]
    # Номер перемещения уже есть в начале комментария — не дублируем его
    # еще раз в конце строки с заявкой.
    assert "REQ-778: PER-BOX-1C-5" not in movement["comment"]


def test_export_comment_without_marketplace_request_number_unchanged(db, client_logged_in):
    _set_token()
    sender = Warehouse(code="WH-1C-13", name="Основной склад")
    city = Warehouse(code="WH-1C-14", name="ОЗОН: Тула", marketplace="ozon", marketplace_city="Тула")
    db.session.add_all([sender, city])
    db.session.commit()
    item = _make_item("9990000009")

    _ship_box(sender, city, item, 1, "BOX-1C-6", client_logged_in)

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()
    movement = next(m for m in data["movements"] if m["number"] == "PER-BOX-1C-6")
    assert "№ заявки МП" not in movement["comment"]


def test_export_groups_supplier_returns_by_receiving_document(db, client_logged_in):
    _set_token()
    wh = Warehouse(code="WH-1C-7", name="Основной склад")
    db.session.add(wh)
    db.session.commit()
    item1 = _make_item("9990000004", "Товар А")
    item2 = _make_item("9990000005", "Товар Б")
    doc = ReceivingDocument(
        number="НАКЛ-777",
        warehouse_id=wh.id,
        supplier="ИП Тестов",
        invoice_file_name="накладная.xlsx",
        order_number="Ш-00105",
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add_all(
        [
            SupplierReturn(
                warehouse_id=wh.id,
                nomenclature_id=item1.id,
                qty=3,
                receiving_document_id=doc.id,
                supplier_name="ИП Тестов",
                invoice_number="НАКЛ-777",
            ),
            SupplierReturn(
                warehouse_id=wh.id,
                nomenclature_id=item2.id,
                qty=1,
                receiving_document_id=doc.id,
                supplier_name="ИП Тестов",
                invoice_number="НАКЛ-777",
            ),
            # Без invoice_number (ручное списание либо приемка без импорта
            # накладной) — не должен попасть в выгрузку вообще.
            SupplierReturn(warehouse_id=wh.id, nomenclature_id=item1.id, qty=9),
        ]
    )
    db.session.commit()

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()

    assert len(data["supplier_returns"]) == 1
    ret_doc = data["supplier_returns"][0]
    assert ret_doc["id"] == doc.id
    assert ret_doc["invoice_number"] == "НАКЛ-777"
    assert ret_doc["order_number"] == "Ш-00105"
    assert ret_doc["supplier"] == "ИП Тестов"
    assert {line["qty"] for line in ret_doc["lines"]} == {3, 1}


def test_export_supplier_return_order_number_uses_first_of_slash_separated_list(db, client_logged_in):
    """Накладная закрывает сразу две заявки (см. чат — например
    «ВБ-К11/ВБ-К10») — возврат привязывается к ПЕРВОЙ из них, даже если по
    факту возвращаемая позиция относится ко второй."""
    _set_token()
    wh = Warehouse(code="WH-1C-20", name="Основной склад")
    db.session.add(wh)
    db.session.commit()
    item = _make_item("9990000020")
    doc = ReceivingDocument(
        number="НАКЛ-900",
        warehouse_id=wh.id,
        invoice_file_name="накладная.xlsx",
        order_number="ВБ-К11/ВБ-К10",
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        SupplierReturn(
            warehouse_id=wh.id,
            nomenclature_id=item.id,
            qty=2,
            receiving_document_id=doc.id,
            invoice_number="НАКЛ-900",
        )
    )
    db.session.commit()

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    ret_doc = resp.get_json()["supplier_returns"][0]
    assert ret_doc["order_number"] == "ВБ-К11"


def test_export_supplier_return_order_number_uses_first_of_comma_separated_list(db, client_logged_in):
    _set_token()
    wh = Warehouse(code="WH-1C-21", name="Основной склад")
    db.session.add(wh)
    db.session.commit()
    item = _make_item("9990000021")
    doc = ReceivingDocument(
        number="НАКЛ-901",
        warehouse_id=wh.id,
        invoice_file_name="накладная.xlsx",
        order_number="ВБ-К11, ВБ-К10",
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        SupplierReturn(
            warehouse_id=wh.id,
            nomenclature_id=item.id,
            qty=2,
            receiving_document_id=doc.id,
            invoice_number="НАКЛ-901",
        )
    )
    db.session.commit()

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    ret_doc = resp.get_json()["supplier_returns"][0]
    assert ret_doc["order_number"] == "ВБ-К11"


def test_export_supplier_return_order_number_without_separator_unchanged(db, client_logged_in):
    _set_token()
    wh = Warehouse(code="WH-1C-22", name="Основной склад")
    db.session.add(wh)
    db.session.commit()
    item = _make_item("9990000022")
    doc = ReceivingDocument(
        number="НАКЛ-902",
        warehouse_id=wh.id,
        invoice_file_name="накладная.xlsx",
        order_number="ШМ-005",
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        SupplierReturn(
            warehouse_id=wh.id,
            nomenclature_id=item.id,
            qty=1,
            receiving_document_id=doc.id,
            invoice_number="НАКЛ-902",
        )
    )
    db.session.commit()

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    ret_doc = resp.get_json()["supplier_returns"][0]
    assert ret_doc["order_number"] == "ШМ-005"


def test_export_includes_receiving_adjustment_when_recount_mismatches_invoice(db, client_logged_in):
    """Приемка из накладной, ушедшая в разбраковку/завершение с расхождением
    (qty != expected_qty хотя бы по одной строке) — 1С должна поправить
    количество в уже заведенной приходной накладной (см.
    SyncWMS.bsl СкорректироватьПриемку)."""
    _set_token()
    wh = Warehouse(code="WH-1C-15", name="Основной склад")
    db.session.add(wh)
    db.session.commit()
    item = _make_item("9990000010")
    doc = ReceivingDocument(
        number="НАКЛ-888",
        warehouse_id=wh.id,
        invoice_file_name="накладная.xlsx",
        status="sorting",
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=7, expected_qty=10))
    db.session.commit()

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()

    assert len(data["receiving_adjustments"]) == 1
    adjustment = data["receiving_adjustments"][0]
    assert adjustment["id"] == doc.id
    assert adjustment["invoice_number"] == "НАКЛ-888"
    assert adjustment["lines"][0]["qty"] == 7


def test_export_excludes_receiving_without_discrepancy(db, client_logged_in):
    _set_token()
    wh = Warehouse(code="WH-1C-16", name="Основной склад")
    db.session.add(wh)
    db.session.commit()
    item = _make_item("9990000011")
    doc = ReceivingDocument(
        number="НАКЛ-889",
        warehouse_id=wh.id,
        invoice_file_name="накладная.xlsx",
        status="completed",
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10, expected_qty=10))
    db.session.commit()

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()

    assert data["receiving_adjustments"] == []


def test_export_excludes_receiving_still_recounting(db, client_logged_in):
    """Пока приемка в статусе draft/recounting, цифры могут еще измениться
    (см. receiving.confirm_line) — рано отправлять корректировку в 1С."""
    _set_token()
    wh = Warehouse(code="WH-1C-17", name="Основной склад")
    db.session.add(wh)
    db.session.commit()
    item = _make_item("9990000012")
    doc = ReceivingDocument(
        number="НАКЛ-890",
        warehouse_id=wh.id,
        invoice_file_name="накладная.xlsx",
        status="recounting",
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=7, expected_qty=10))
    db.session.commit()

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()

    assert data["receiving_adjustments"] == []


def test_export_confirm_marks_receiving_adjustment_synced(db, client_logged_in):
    _set_token()
    wh = Warehouse(code="WH-1C-18", name="Основной склад")
    db.session.add(wh)
    db.session.commit()
    item = _make_item("9990000013")
    doc = ReceivingDocument(
        number="НАКЛ-891",
        warehouse_id=wh.id,
        invoice_file_name="накладная.xlsx",
        status="completed",
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=5, expected_qty=8))
    db.session.commit()

    resp = client_logged_in.post(
        "/integrations/1c/api/export/confirm",
        data=json.dumps({"receiving_adjustment_ids": [doc.id]}),
        headers={"X-1C-Token": TOKEN, "Content-Type": "application/json"},
    )
    assert resp.get_json()["confirmed"]["receiving_adjustments"] == 1
    assert ReceivingDocument.query.get(doc.id).recount_synced_to_1c_at is not None

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    assert resp.get_json()["receiving_adjustments"] == []


def test_export_confirm_marks_everything_synced(db, client_logged_in):
    _set_token()
    wh = Warehouse(code="WH-1C-8", name="Основной склад")
    db.session.add(wh)
    db.session.commit()
    item = _make_item("9990000006")
    doc = ReceivingDocument(
        number="НАКЛ-888", warehouse_id=wh.id, supplier="ИП Тестов", invoice_file_name="накладная.xlsx"
    )
    db.session.add(doc)
    db.session.commit()
    ret = SupplierReturn(
        warehouse_id=wh.id,
        nomenclature_id=item.id,
        qty=2,
        receiving_document_id=doc.id,
        supplier_name="ИП Тестов",
        invoice_number="НАКЛ-888",
    )
    db.session.add(ret)
    db.session.commit()

    resp = client_logged_in.post(
        "/integrations/1c/api/export/confirm",
        data=json.dumps({"movement_ids": [], "inventory_ids": [], "supplier_return_ids": [doc.id]}),
        content_type="application/json",
        headers={"X-1C-Token": TOKEN},
    )
    assert resp.get_json()["confirmed"]["supplier_returns"] == 1
    assert SupplierReturn.query.get(ret.id).synced_to_1c_at is not None

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    assert resp.get_json()["supplier_returns"] == []


def test_export_confirm_auto_ticks_accounting_checkbox_for_movements(db, client_logged_in):
    """Успешное подтверждение от 1С само ставит галочку бухгалтера "внесено
    в 1С" — чтобы не дублировать вручную то, что и так подтвердила
    интеграция (см. accounting_entered_at в модели)."""
    _set_token()
    sender = Warehouse(code="WH-1C-9", name="Основной склад")
    receiver = Warehouse(code="WH-1C-10", name="Склад №2 (Шоссейная 167)")
    db.session.add_all([sender, receiver])
    db.session.commit()
    item = _make_item("9990000007")
    doc = _ship_box(sender, receiver, item, 3, "BOX-1C-CHK", client_logged_in)
    assert doc.accounting_entered_at is None

    resp = client_logged_in.post(
        "/integrations/1c/api/export/confirm",
        data=json.dumps({"movement_ids": [doc.id], "inventory_ids": [], "supplier_return_ids": []}),
        content_type="application/json",
        headers={"X-1C-Token": TOKEN},
    )
    assert resp.get_json()["confirmed"]["movements"] == 1

    doc = MovementDocument.query.get(doc.id)
    assert doc.synced_to_1c_at is not None
    assert doc.accounting_entered_at is not None


def test_export_confirm_stores_movement_warning(db, client_logged_in):
    """1С может подтвердить документ, но сообщить, что часть строк не
    сопоставилась с номенклатурой и была пропущена (см. SyncWMS.bsl
    СоздатьПеремещениеТоваров) — WMS сохраняет текст предупреждения, чтобы
    показать "!" в списке перемещений."""
    _set_token()
    sender = Warehouse(code="WH-1C-11", name="Основной склад")
    receiver = Warehouse(code="WH-1C-12", name="Склад №2 (Шоссейная 167)")
    db.session.add_all([sender, receiver])
    db.session.commit()
    item = _make_item("9990000008")
    doc = _ship_box(sender, receiver, item, 3, "BOX-1C-WARN", client_logged_in)

    resp = client_logged_in.post(
        "/integrations/1c/api/export/confirm",
        data=json.dumps(
            {
                "movement_ids": [doc.id],
                "inventory_ids": [],
                "supplier_return_ids": [],
                "movement_warnings": {str(doc.id): "строка 2 не сопоставлена (barcode=\"999\")"},
            }
        ),
        content_type="application/json",
        headers={"X-1C-Token": TOKEN},
    )
    assert resp.get_json()["confirmed"]["movements"] == 1

    doc = MovementDocument.query.get(doc.id)
    assert doc.synced_to_1c_at is not None
    assert doc.sync_warning == 'строка 2 не сопоставлена (barcode="999")'


def test_export_confirm_without_warnings_leaves_sync_warning_empty(db, client_logged_in):
    _set_token()
    sender = Warehouse(code="WH-1C-13", name="Основной склад")
    receiver = Warehouse(code="WH-1C-14", name="Склад №2 (Шоссейная 167)")
    db.session.add_all([sender, receiver])
    db.session.commit()
    item = _make_item("9990000009")
    doc = _ship_box(sender, receiver, item, 3, "BOX-1C-NOWARN", client_logged_in)

    client_logged_in.post(
        "/integrations/1c/api/export/confirm",
        data=json.dumps({"movement_ids": [doc.id], "inventory_ids": [], "supplier_return_ids": []}),
        content_type="application/json",
        headers={"X-1C-Token": TOKEN},
    )

    assert MovementDocument.query.get(doc.id).sync_warning is None
