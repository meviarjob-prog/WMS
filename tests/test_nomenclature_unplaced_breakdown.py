"""Номенклатура и «Где товар»: неразмещенная часть остатка показана
отдельно, с источником (приемка или без документа)."""

from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, UnplacedStock, Warehouse


def _wh(name, code):
    wh = Warehouse(name=name, code=code)
    db.session.add(wh)
    db.session.flush()
    return wh


def test_unplaced_part_shown_separately(db, client_logged_in):
    shosse = _wh("Склад №2 (Шоссейная 167)", "S2")
    kazan = _wh("Казань", "KZN")
    item = Nomenclature(name="кардиган синий", sku="kard-1", barcode="2056744446961", unit="шт")
    db.session.add(item)
    db.session.flush()
    for wh, number in ((shosse, "BOX-1"), (kazan, "BOX-2")):
        box = Box(box_number=number, warehouse_id=wh.id)
        db.session.add(box)
        db.session.flush()
        db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=40 if wh is shosse else 20))
    UnplacedStock.add(shosse.id, item.id, 40)
    db.session.commit()

    page = client_logged_in.get("/nomenclature/?q=кардиган").get_data(as_text=True)
    assert "80 шт" in page and "неразм. 40" in page

    page = client_logged_in.get("/nomenclature/locate?barcode=2056744446961").get_data(as_text=True)
    assert "без документа приемки" in page


def test_admin_writes_off_unplaced_to_fact(db, client_logged_in):
    wh = _wh("Основной склад", "MAIN")
    item = Nomenclature(name="кардиган", sku="k-2", barcode="2056700000002", unit="шт")
    db.session.add(item)
    db.session.flush()
    UnplacedStock.add(wh.id, item.id, 60)
    db.session.commit()
    row = UnplacedStock.query.one()
    page = client_logged_in.get("/nomenclature/locate?barcode=2056700000002").get_data(as_text=True)
    assert "Списать до факта" in page
    client_logged_in.post(f"/nomenclature/locate/unplaced/{row.id}/set", data={"qty": "100"})
    assert UnplacedStock.available(wh.id, item.id) == 60  # увеличить нельзя
    client_logged_in.post(f"/nomenclature/locate/unplaced/{row.id}/set", data={"qty": "0"})
    assert UnplacedStock.available(wh.id, item.id) == 0
