"""Коррекция уже выгруженного в 1С перемещения — если состав меняют ПОСЛЕ
синхронизации (добавили/удалили короб — movement.add_box/delete_line, или
поправили количество в уже уехавшем коробе — boxes.add_item/update_item/
move_item/delete_item), см. чат: "при изменении данных в перемещениях тоже
необходимо синхронизировать изменения в 1с". Помечается
MovementDocument.composition_changed_at, выгружается через
/integrations/1c/api/export ("movement_corrections") и подтверждается через
/integrations/1c/api/export/confirm ("movement_correction_ids") — см.
integration_1c._movement_corrections_export/_candidates и SyncWMS.bsl
НайтиПеремещениеПоНомеруWMS/СкорректироватьПеремещение (поиск документа в
1С по номеру WMS, зашитому в поле "Комментарий" — у перемещения нет своего
реквизита-номера, как у накладной)."""

import json
from datetime import datetime

from wms.extensions import db
from wms.models import AppSetting, Box, BoxItem, MovementDocument, MovementLine, Nomenclature, Warehouse

TOKEN = "test-1c-token-mvcorr"


def _set_token():
    db.session.add(AppSetting(key="api_1c_token", value=TOKEN))
    db.session.commit()


def _make_item(barcode, name="Товар"):
    item = Nomenclature(sku=barcode, barcode=barcode, name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_synced_movement(sender, receiver, item, qty, number, box_number):
    """Уже выгруженный в 1С документ (synced_to_1c_at заполнен) с одним
    коробом на складе назначения — как будто 1С уже забрала документ."""
    box = Box(box_number=box_number, warehouse_id=receiver.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
    doc = MovementDocument(
        number=number,
        from_warehouse_id=sender.id,
        to_warehouse_id=receiver.id,
        status="completed",
        completed_at=datetime.utcnow(),
        synced_to_1c_at=datetime.utcnow(),
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id)
    )
    db.session.commit()
    return doc, box


def test_add_box_to_synced_movement_flags_correction(db, client_logged_in):
    sender = Warehouse(code="WH-MVC-1", name="Склад-отправитель 1")
    receiver = Warehouse(code="WH-MVC-2", name="Склад-получатель 1")
    db.session.add_all([sender, receiver])
    db.session.commit()
    item = _make_item("8880000001")
    doc, _box = _make_synced_movement(sender, receiver, item, 5, "MVC-0001", "BOX-MVC-1")

    new_box = Box(box_number="BOX-MVC-2", warehouse_id=sender.id, status="open")
    db.session.add(new_box)
    db.session.commit()
    db.session.add(BoxItem(box_id=new_box.id, nomenclature_id=item.id, qty=2))
    db.session.commit()

    assert doc.composition_changed_at is None

    resp = client_logged_in.post(
        f"/movement/{doc.id}/boxes/add", data={"box_number": "BOX-MVC-2"}
    )
    assert resp.status_code in (302, 200)

    doc = MovementDocument.query.get(doc.id)
    assert doc.composition_changed_at is not None
    assert doc.synced_to_1c_at is not None


def test_add_box_to_unsynced_draft_does_not_flag_correction(db, client_logged_in):
    """Обычное добавление короба в черновик (еще не выгруженный) — не
    коррекция, просто первичное составление документа."""
    sender = Warehouse(code="WH-MVC-3", name="Склад-отправитель 2")
    receiver = Warehouse(code="WH-MVC-4", name="Склад-получатель 2")
    db.session.add_all([sender, receiver])
    db.session.commit()
    item = _make_item("8880000002")

    box = Box(box_number="BOX-MVC-3", warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=1))
    doc = MovementDocument(number="MVC-0002", from_warehouse_id=sender.id, to_warehouse_id=receiver.id)
    db.session.add(doc)
    db.session.commit()

    client_logged_in.post(f"/movement/{doc.id}/boxes/add", data={"box_number": "BOX-MVC-3"})

    doc = MovementDocument.query.get(doc.id)
    assert doc.composition_changed_at is None


def test_delete_line_from_synced_movement_flags_correction(db, client_logged_in):
    sender = Warehouse(code="WH-MVC-5", name="Склад-отправитель 3")
    receiver = Warehouse(code="WH-MVC-6", name="Склад-получатель 3")
    db.session.add_all([sender, receiver])
    db.session.commit()
    item = _make_item("8880000003")
    doc, box = _make_synced_movement(sender, receiver, item, 4, "MVC-0003", "BOX-MVC-4")
    line = doc.lines.first()

    resp = client_logged_in.post(f"/movement/{doc.id}/lines/{line.id}/delete")
    assert resp.status_code in (302, 200)

    doc = MovementDocument.query.get(doc.id)
    assert doc.composition_changed_at is not None


def test_box_update_item_qty_flags_correction_for_synced_movement(db, client_logged_in):
    """Правка количества прямо в коробе (админ, см. boxes.update_item) — тот
    самый сценарий "меняем кол-во" из чата."""
    sender = Warehouse(code="WH-MVC-7", name="Склад-отправитель 4")
    receiver = Warehouse(code="WH-MVC-8", name="Склад-получатель 4")
    db.session.add_all([sender, receiver])
    db.session.commit()
    item = _make_item("8880000004")
    doc, box = _make_synced_movement(sender, receiver, item, 6, "MVC-0004", "BOX-MVC-5")
    box_item = box.items.first()

    resp = client_logged_in.post(
        f"/boxes/{box.id}/items/{box_item.id}/update", data={"qty": "9"}
    )
    assert resp.status_code in (302, 200)

    doc = MovementDocument.query.get(doc.id)
    assert doc.composition_changed_at is not None


def test_box_update_item_qty_on_box_without_synced_movement_does_not_flag(db, client_logged_in):
    wh = Warehouse(code="WH-MVC-9", name="Склад без перемещения")
    db.session.add(wh)
    db.session.commit()
    item = _make_item("8880000005")
    box = Box(box_number="BOX-MVC-6", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    box_item = BoxItem(box_id=box.id, nomenclature_id=item.id, qty=1)
    db.session.add(box_item)
    db.session.commit()

    client_logged_in.post(f"/boxes/{box.id}/items/{box_item.id}/update", data={"qty": "3"})

    # Ничего не падает и ни у одного перемещения ничего не проставляется —
    # просто нет ни одной строки перемещения на этот короб.
    assert MovementDocument.query.count() == 0


def test_export_includes_movement_correction_with_current_lines(db, client_logged_in):
    _set_token()
    sender = Warehouse(code="WH-MVC-10", name="Склад-отправитель 5")
    receiver = Warehouse(code="WH-MVC-11", name="Склад-получатель 5")
    db.session.add_all([sender, receiver])
    db.session.commit()
    item = _make_item("8880000006", "Товар для коррекции")
    doc, box = _make_synced_movement(sender, receiver, item, 5, "MVC-0005", "BOX-MVC-7")
    box_item = box.items.first()
    box_item.qty = 8
    doc.composition_changed_at = datetime.utcnow()
    db.session.commit()

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()

    assert len(data["movement_corrections"]) == 1
    correction = data["movement_corrections"][0]
    assert correction["id"] == doc.id
    assert correction["number"] == "MVC-0005"
    assert correction["lines"] == [{"barcode": "8880000006", "name": "Товар для коррекции", "qty": 8}]


def test_export_excludes_synced_movement_without_composition_change(db, client_logged_in):
    _set_token()
    sender = Warehouse(code="WH-MVC-12", name="Склад-отправитель 6")
    receiver = Warehouse(code="WH-MVC-13", name="Склад-получатель 6")
    db.session.add_all([sender, receiver])
    db.session.commit()
    item = _make_item("8880000007")
    _make_synced_movement(sender, receiver, item, 1, "MVC-0006", "BOX-MVC-8")

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    data = resp.get_json()

    assert data["movement_corrections"] == []


def test_export_confirm_clears_composition_changed_but_keeps_synced(db, client_logged_in):
    _set_token()
    sender = Warehouse(code="WH-MVC-14", name="Склад-отправитель 7")
    receiver = Warehouse(code="WH-MVC-15", name="Склад-получатель 7")
    db.session.add_all([sender, receiver])
    db.session.commit()
    item = _make_item("8880000008")
    doc, _box = _make_synced_movement(sender, receiver, item, 2, "MVC-0007", "BOX-MVC-9")
    synced_at_before = doc.synced_to_1c_at
    doc.composition_changed_at = datetime.utcnow()
    db.session.commit()

    resp = client_logged_in.post(
        "/integrations/1c/api/export/confirm",
        data=json.dumps({"movement_correction_ids": [doc.id]}),
        content_type="application/json",
        headers={"X-1C-Token": TOKEN},
    )
    assert resp.get_json()["confirmed"]["movement_corrections"] == 1

    doc = MovementDocument.query.get(doc.id)
    assert doc.composition_changed_at is None
    assert doc.synced_to_1c_at == synced_at_before

    resp = client_logged_in.get("/integrations/1c/api/export", headers={"X-1C-Token": TOKEN})
    assert resp.get_json()["movement_corrections"] == []


def test_pending_page_lists_movement_correction_and_toggle_clears_it(db, client_logged_in):
    sender = Warehouse(code="WH-MVC-16", name="Склад-отправитель 8")
    receiver = Warehouse(code="WH-MVC-17", name="Склад-получатель 8")
    db.session.add_all([sender, receiver])
    db.session.commit()
    item = _make_item("8880000009")
    doc, _box = _make_synced_movement(sender, receiver, item, 3, "MVC-0008", "BOX-MVC-10")
    doc.composition_changed_at = datetime.utcnow()
    db.session.commit()

    html = client_logged_in.get("/integrations/1c/pending").get_data(as_text=True)
    assert "MVC-0008" in html

    resp = client_logged_in.post(f"/integrations/1c/pending/movement-correction/{doc.id}/toggle")
    assert resp.status_code == 302

    doc = MovementDocument.query.get(doc.id)
    assert doc.composition_changed_at is None
