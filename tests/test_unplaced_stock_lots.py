"""Партии неразмещенного остатка (UnplacedStockLot) — чтобы по остатку было
видно, от какого поставщика и по какой заявке он пришел (см. обсуждение:
UnplacedStock остается быстрым агрегатом, партии списываются по очереди
поступления, FIFO). А также дедупликация при приемке сразу в короб — если
у товара уже есть неразмещенный остаток, короб сначала списывает его,
вместо того чтобы задваивать (см. receiving._receive_item_into_box)."""

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    Nomenclature,
    PlacementDocument,
    ReceivingDocument,
    UnplacedStock,
    UnplacedStockLot,
    Warehouse,
)


def _make_warehouse(code="WH-LOT"):
    wh = Warehouse(code=code, name="Тест склад партий")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(barcode, name="Товар партий"):
    item = Nomenclature(sku=barcode, barcode=barcode, name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def test_add_without_receiving_document_creates_lot_without_provenance(db):
    wh = _make_warehouse("WH-LOT-1")
    item = _make_item("7770000001")

    UnplacedStock.add(wh.id, item.id, 10)
    db.session.commit()

    lot = UnplacedStockLot.query.filter_by(warehouse_id=wh.id, nomenclature_id=item.id).first()
    assert lot.qty_received == 10
    assert lot.qty_remaining == 10
    assert lot.supplier_name is None
    assert lot.order_number is None


def test_add_with_receiving_document_snapshots_supplier_and_order_number(db):
    wh = _make_warehouse("WH-LOT-2")
    item = _make_item("7770000002")
    doc = ReceivingDocument(
        number="LOT-DOC-1", warehouse_id=wh.id, supplier="ООО Ромашка", order_number="ЗАЯВ-9"
    )
    db.session.add(doc)
    db.session.commit()

    UnplacedStock.add(wh.id, item.id, 15, receiving_document=doc)
    db.session.commit()

    lot = UnplacedStockLot.query.filter_by(warehouse_id=wh.id, nomenclature_id=item.id).first()
    assert lot.supplier_name == "ООО Ромашка"
    assert lot.order_number == "ЗАЯВ-9"
    assert lot.receiving_document_id == doc.id


def test_consume_draws_down_fifo_across_multiple_lots(db):
    wh = _make_warehouse("WH-LOT-3")
    item = _make_item("7770000003")

    UnplacedStock.add(wh.id, item.id, 5)  # первая партия
    db.session.commit()
    UnplacedStock.add(wh.id, item.id, 5)  # вторая партия
    db.session.commit()

    UnplacedStock.consume(wh.id, item.id, 7)
    db.session.commit()

    lots = (
        UnplacedStockLot.query.filter_by(warehouse_id=wh.id, nomenclature_id=item.id)
        .order_by(UnplacedStockLot.id)
        .all()
    )
    assert lots[0].qty_remaining == 0  # первая партия списана полностью
    assert lots[1].qty_remaining == 3  # вторая — частично
    assert UnplacedStock.available(wh.id, item.id) == 3


def test_consume_stops_when_lots_run_out_without_going_negative(db):
    wh = _make_warehouse("WH-LOT-4")
    item = _make_item("7770000004")
    UnplacedStock.add(wh.id, item.id, 3)
    db.session.commit()

    UnplacedStock.consume(wh.id, item.id, 3)
    db.session.commit()

    lot = UnplacedStockLot.query.filter_by(warehouse_id=wh.id, nomenclature_id=item.id).first()
    assert lot.qty_remaining == 0
    assert UnplacedStock.available(wh.id, item.id) == 0


def test_active_lots_excludes_fully_consumed(db):
    wh = _make_warehouse("WH-LOT-5")
    item = _make_item("7770000005")
    UnplacedStock.add(wh.id, item.id, 5)
    db.session.commit()
    UnplacedStock.add(wh.id, item.id, 5)
    db.session.commit()
    UnplacedStock.consume(wh.id, item.id, 5)
    db.session.commit()

    row = UnplacedStock.query.filter_by(warehouse_id=wh.id, nomenclature_id=item.id).first()
    active = row.active_lots()

    assert len(active) == 1
    assert active[0].qty_remaining == 5


def test_receiving_complete_creates_lot_with_document_provenance(db, client_logged_in):
    wh = _make_warehouse("WH-LOT-6")
    item = _make_item("7770000006")
    doc = ReceivingDocument(
        number="LOT-DOC-2", warehouse_id=wh.id, supplier="ИП Тестов", order_number="З-100"
    )
    db.session.add(doc)
    db.session.commit()
    from wms.models import ReceivingLine

    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=20))
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/send-to-recount")
    client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting")
    client_logged_in.post(f"/receiving/{doc.id}/complete")

    lot = UnplacedStockLot.query.filter_by(warehouse_id=wh.id, nomenclature_id=item.id).first()
    assert lot is not None
    assert lot.qty_received == 20
    assert lot.supplier_name == "ИП Тестов"
    assert lot.order_number == "З-100"
    assert lot.receiving_document_id == doc.id


def test_receiving_into_box_consumes_existing_unplaced_stock_first(db, client_logged_in):
    """Если товар уже лежит неразмещенным (от прошлой приемки), а сейчас его
    же пакуют в короб в рамках НОВОЙ приемки — списываем со старого
    остатка, а не заводим его как будто это отдельная новая партия."""
    wh = _make_warehouse("WH-LOT-7")
    item = _make_item("7770000007")

    UnplacedStock.add(wh.id, item.id, 6)  # старый остаток, скажем, от прошлой приемки
    db.session.commit()
    assert UnplacedStock.available(wh.id, item.id) == 6

    box = Box(box_number="BOX-LOT-7", warehouse_id=wh.id, status="open")
    db.session.add(box)
    new_doc = ReceivingDocument(number="LOT-DOC-3", warehouse_id=wh.id)
    db.session.add(new_doc)
    db.session.commit()

    client_logged_in.post(
        f"/receiving/{new_doc.id}/boxes/{box.id}/lines/add-by-barcode",
        json={"barcode": item.barcode, "qty": 4},
    )

    # Старый остаток уменьшился на упакованное количество — не задвоился.
    assert UnplacedStock.available(wh.id, item.id) == 2
    box_item = BoxItem.query.filter_by(box_id=box.id, nomenclature_id=item.id).first()
    assert box_item.qty == 4


def test_receiving_into_box_beyond_unplaced_stock_does_not_go_negative(db, client_logged_in):
    """Если пакуют больше, чем было неразмещенного остатка — списываем
    сколько было (до нуля), остальное просто новое поступление, без ошибок."""
    wh = _make_warehouse("WH-LOT-8")
    item = _make_item("7770000008")
    UnplacedStock.add(wh.id, item.id, 2)
    db.session.commit()

    box = Box(box_number="BOX-LOT-8", warehouse_id=wh.id, status="open")
    db.session.add(box)
    new_doc = ReceivingDocument(number="LOT-DOC-4", warehouse_id=wh.id)
    db.session.add(new_doc)
    db.session.commit()

    client_logged_in.post(
        f"/receiving/{new_doc.id}/boxes/{box.id}/lines/add-by-barcode",
        json={"barcode": item.barcode, "qty": 10},
    )

    assert UnplacedStock.available(wh.id, item.id) == 0
    box_item = BoxItem.query.filter_by(box_id=box.id, nomenclature_id=item.id).first()
    assert box_item.qty == 10


def test_placement_scan_into_box_consumes_lot(db, client_logged_in):
    wh = _make_warehouse("WH-LOT-9")
    item = _make_item("7770000009")
    receiving_doc = ReceivingDocument(number="LOT-DOC-5", warehouse_id=wh.id, supplier="Поставщик Y")
    db.session.add(receiving_doc)
    db.session.commit()
    UnplacedStock.add(wh.id, item.id, 8, receiving_document=receiving_doc)
    db.session.commit()

    box = Box(box_number="BOX-LOT-9", warehouse_id=wh.id, status="open")
    placement_doc = PlacementDocument(number="RAZ-LOT-1", warehouse_id=wh.id)
    db.session.add_all([box, placement_doc])
    db.session.commit()

    client_logged_in.post(
        f"/placement/{placement_doc.id}/boxes/{box.id}/items/add-by-barcode",
        json={"barcode": item.barcode, "qty": 5},
    )

    assert UnplacedStock.available(wh.id, item.id) == 3
    lot = UnplacedStockLot.query.filter_by(warehouse_id=wh.id, nomenclature_id=item.id).first()
    assert lot.qty_remaining == 3
