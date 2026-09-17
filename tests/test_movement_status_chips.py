"""Статусы перемещения показаны 4 небольшими плашками рядом (на сборке/
собрано/создана заявка/отгружено) вместо одного бейджа — тот же принцип,
что и у приемки (см. test_receiving_status_chips.py), только плашки чуть
шире (status-chip-wide, двухбуквенные подписи). "Пройден ли этап" кодируется
классом status-chip-reached и подсказкой с датой перехода."""

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, MovementLine, Nomenclature, Warehouse


def _make_warehouses(suffix):
    sender = Warehouse(code=f"WH-MCHIP{suffix}A", name="Склад-отправитель")
    dest = Warehouse(code=f"WH-MCHIP{suffix}B", name="Склад назначения")
    db.session.add_all([sender, dest])
    db.session.commit()
    return sender, dest


def _make_doc_with_box(sender, dest, number, box_number):
    item = Nomenclature(sku=f"SKU-{number}", barcode=f"7770900{number}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    box = Box(box_number=box_number, warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=1))

    doc = MovementDocument(number=number, from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
    db.session.commit()
    return doc, item


def test_draft_document_shows_only_onassembly_chip_reached(db, client_logged_in):
    sender, dest = _make_warehouses("1")
    doc, _item = _make_doc_with_box(sender, dest, "MCHIP-0001", "BOX-MCHIP1")

    html = client_logged_in.get(f"/movement/{doc.id}").get_data(as_text=True)

    assert "status-chip-onassembly status-chip-reached" in html
    assert "status-chip-assembled status-chip-reached" not in html
    assert "status-chip-requested status-chip-reached" not in html
    assert "status-chip-shipped status-chip-reached" not in html


def test_completed_document_shows_onassembly_and_assembled_reached(db, client_logged_in):
    sender, dest = _make_warehouses("2")
    doc, _item = _make_doc_with_box(sender, dest, "MCHIP-0002", "BOX-MCHIP2")

    client_logged_in.post(f"/movement/{doc.id}/complete")

    html = client_logged_in.get(f"/movement/{doc.id}").get_data(as_text=True)

    assert "status-chip-onassembly status-chip-reached" in html
    assert "status-chip-assembled status-chip-reached" in html
    assert "status-chip-requested status-chip-reached" not in html
    assert "status-chip-shipped status-chip-reached" not in html


def test_marketplace_request_reaches_requested_chip_not_shipped(db, client_logged_in):
    sender, dest = _make_warehouses("3")
    doc, _item = _make_doc_with_box(sender, dest, "MCHIP-0003", "BOX-MCHIP3")

    client_logged_in.post(f"/movement/{doc.id}/complete")
    client_logged_in.post(f"/movement/{doc.id}/toggle-marketplace-request")

    html = client_logged_in.get(f"/movement/{doc.id}").get_data(as_text=True)

    assert "status-chip-requested status-chip-reached" in html
    assert "status-chip-shipped status-chip-reached" not in html


def test_received_document_reaches_shipped_chip(db, client_logged_in):
    sender, dest = _make_warehouses("4")
    doc, item = _make_doc_with_box(sender, dest, "MCHIP-0004", "BOX-MCHIP4")

    client_logged_in.post(f"/movement/{doc.id}/complete")
    client_logged_in.post(f"/movement/{doc.id}/toggle-marketplace-request")
    client_logged_in.post(f"/movement/{doc.id}/receive", data={f"qty_{item.id}": "1"})

    html = client_logged_in.get(f"/movement/{doc.id}").get_data(as_text=True)

    assert "status-chip-shipped status-chip-reached" in html


def test_list_page_shows_status_chips(db, client_logged_in):
    sender, dest = _make_warehouses("5")
    _make_doc_with_box(sender, dest, "MCHIP-0005", "BOX-MCHIP5")

    html = client_logged_in.get("/movement/").get_data(as_text=True)

    assert "status-chips" in html
    assert "status-chip-onassembly status-chip-reached" in html


def test_list_page_has_per_column_text_filters(db, client_logged_in):
    sender, dest = _make_warehouses("6")
    _make_doc_with_box(sender, dest, "MCHIP-0006", "BOX-MCHIP6")

    html = client_logged_in.get("/movement/").get_data(as_text=True)

    assert "col-filter" in html
    assert 'data-col="1"' in html
