from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, ReceivingDocument, ReceivingLine, Warehouse


def _setup():
    warehouse = Warehouse(code="WH-IDEMP", name="Основной")
    item = Nomenclature(
        sku="SKU-IDEMP", barcode="4600000000999", name="Товар от повторов", unit="шт"
    )
    db.session.add_all([warehouse, item])
    db.session.commit()
    doc = ReceivingDocument(number="REC-IDEMP", warehouse_id=warehouse.id)
    db.session.add(doc)
    db.session.commit()
    return warehouse, item, doc


def test_repeated_add_request_does_not_duplicate_receiving_line(db, client_logged_in):
    _warehouse, item, doc = _setup()
    data = {"nomenclature_id": item.id, "qty": "4", "request_token": "same-click-1"}

    client_logged_in.post(f"/receiving/{doc.id}/lines/add", data=data)
    client_logged_in.post(f"/receiving/{doc.id}/lines/add", data=data)

    lines = ReceivingLine.query.filter_by(document_id=doc.id).all()
    assert len(lines) == 1
    assert lines[0].qty == 4


def test_repeated_add_to_box_does_not_increase_box_qty_twice(db, client_logged_in, admin_user):
    warehouse, item, doc = _setup()
    box = Box(box_number="BOX-IDEMP", warehouse_id=warehouse.id, status="open")
    db.session.add(box)
    db.session.commit()
    data = {"nomenclature_id": item.id, "qty": "3", "request_token": "same-click-box-1"}

    url = f"/receiving/{doc.id}/boxes/{box.id}/lines/add"
    client_logged_in.post(url, data=data)
    client_logged_in.post(url, data=data)

    box_item = BoxItem.query.filter_by(box_id=box.id, nomenclature_id=item.id).one()
    assert box_item.qty == 3
    line = ReceivingLine.query.filter_by(document_id=doc.id).one()
    assert line.received_by_id == admin_user.id

    box_html = client_logged_in.get(f"/boxes/{box.id}").get_data(as_text=True)
    list_html = client_logged_in.get("/boxes/").get_data(as_text=True)
    assert "Принимал" in box_html and admin_user.display_name() in box_html
    assert "Принимал" in list_html and admin_user.display_name() in list_html


def test_receiving_add_form_blocks_double_submit_in_browser(db, client_logged_in):
    _warehouse, _item, doc = _setup()

    html = client_logged_in.get(f"/receiving/{doc.id}").get_data(as_text=True)

    assert "data-single-submit" in html
    assert 'name="request_token"' in html
