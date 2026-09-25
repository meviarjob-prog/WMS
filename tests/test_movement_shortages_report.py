"""Отчет «Недовозы по перемещениям» (/reports/movement-shortages) —
колонка "Отправлено" должна показывать total_sent_qty() (снимок на момент
завершения сборки), а не "живой" total_item_qty(): если содержимое короба
поправили уже после того, как перемещение уехало и было принято, "живое"
количество отражает правку и расходится с тем, что реально было отправлено
(см. чат: "экспорт показывает 2220, строка показывает 2147" — тот же баг,
здесь для отдельного отчета о недовозах)."""

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


def test_shortages_report_shows_sent_snapshot_not_live_qty(db, client_logged_in):
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
        sent_qty_snapshot=10,  # зафиксировано при завершении сборки, до правки короба
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

    # Короб поправили постфактум — "живое" total_item_qty() теперь больше снимка.
    box.items.first().qty = 55
    db.session.commit()
    assert doc.total_item_qty() == 55

    html = client_logged_in.get("/reports/movement-shortages").get_data(as_text=True)

    idx = html.find(doc.number)
    assert idx != -1
    tail = html[idx:idx + 600]
    assert ">10<" in tail  # снимок на момент отправки
    assert ">55<" not in tail  # не "живое" количество после правки
