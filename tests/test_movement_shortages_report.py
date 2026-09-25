"""Отчет «Недовозы по перемещениям» (/reports/movement-shortages) —
колонка "Отправлено" должна показывать актуальное total_sent_qty() (то же
самое, что и total_item_qty()), а не замороженный на момент отправки
sent_qty_snapshot: эти цифры сверяют с заявками на самом маркетплейсе,
поэтому нужны актуальные данные, а не то, что было отправлено изначально
(см. чат: "экспорт показывает 2220, строка показывает 2147")."""

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


def test_shortages_report_shows_live_qty_not_stale_snapshot(db, client_logged_in):
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
        sent_qty_snapshot=10,  # старый снимок на момент завершения сборки — больше не читается
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

    # Короб поправили постфактум — "живое" количество теперь другое.
    box.items.first().qty = 55
    db.session.commit()
    assert doc.total_item_qty() == 55

    html = client_logged_in.get("/reports/movement-shortages").get_data(as_text=True)

    idx = html.find(doc.number)
    assert idx != -1
    tail = html[idx:idx + 600]
    assert ">55<" in tail  # актуальное количество
    assert ">10<" not in tail  # не старый снимок
