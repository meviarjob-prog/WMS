"""Отчет «Недовозы по перемещениям» (/reports/movement-shortages) —
колонка "Отправлено" после приемки должна показывать зафиксированный на
момент приемки snapshot (sent_qty_snapshot), а не текущее "живое"
содержимое короба: правка короба уже ПОСЛЕ приемки по другой причине
(например, нашли ошибку задним числом) не должна задним числом менять
то, что когда-то фактически отправили/приняли (см. чат: "если поменять
кол-во в перемещении, он минусует еще больше — не должен быть привязан к
основному перемещению"). До приемки отчет по-прежнему live (см.
test_movement_summary_export.test_summary_export_kolvo_v_korobah_shows_live_qty_not_stale_snapshot)."""

from datetime import datetime

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    MovementReceiptDiscrepancy,
    Nomenclature,
    Warehouse,
)


def test_shortages_report_shows_snapshot_not_live_qty_after_receipt(db, client_logged_in):
    sender = Warehouse(code="WH-SHORT-A", name="Отправитель")
    dest = Warehouse(code="WH-SHORT-B", name="ОЗОН: Тест", marketplace="ozon", marketplace_city="Тест")
    db.session.add_all([sender, dest])
    db.session.commit()

    item = Nomenclature(sku="SKU-SHORT-1", barcode="8880000001", name="Товар с недовозом", unit="шт")
    db.session.add(item)
    db.session.commit()

    box = Box(box_number="BOX-SHORT-1", warehouse_id=sender.id, status="stored")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=10))

    doc = MovementDocument(
        number="PER-SHORT-1",
        from_warehouse_id=sender.id,
        to_warehouse_id=dest.id,
        status="completed",
        received_at=datetime.utcnow(),
        sent_qty_snapshot=10,  # зафиксировано на момент приемки
        received_qty_snapshot=7,  # зафиксировано на момент приемки
    )
    db.session.add(doc)
    db.session.commit()
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id))
    db.session.add(
        MovementReceiptDiscrepancy(
            document_id=doc.id, nomenclature_id=item.id, expected_qty=10, received_qty=7,
        )
    )
    db.session.commit()

    # Короб поправили постфактум, уже после приемки, по другой причине —
    # "живое" количество теперь другое, но отчет должен остаться прежним.
    box.items.first().qty = 55
    db.session.commit()
    assert doc.total_item_qty() == 55

    html = client_logged_in.get("/reports/movement-shortages").get_data(as_text=True)

    idx = html.find(doc.number)
    assert idx != -1
    tail = html[idx:idx + 600]
    assert ">10<" in tail  # отправлено — зафиксированный снимок
    assert ">7<" in tail  # принято — зафиксированный снимок
    assert ">55<" not in tail  # не текущее содержимое короба
