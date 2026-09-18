"""Статусы приемки показаны 4 небольшими плашками рядом (черновик/
пересчет/разбраковка/завершено) вместо одного бейджа — цвет плашки
фиксирован по этапу (серая/оранж/желтая/зеленая), а "пройден ли этап"
кодируется классом status-chip-reached (см. style.css) и подсказкой с
датой перехода — чтобы сравнивать скорость прохождения этапов между
приемками."""

from wms.extensions import db
from wms.models import Nomenclature, ReceivingDocument, ReceivingLine, Warehouse


def _make_warehouse(suffix):
    wh = Warehouse(code=f"WH-CHIP{suffix}", name="Склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(suffix):
    item = Nomenclature(sku=f"SKU-CHIP{suffix}", barcode=f"77708000{suffix}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def test_draft_document_shows_only_draft_chip_reached(db, client_logged_in):
    wh = _make_warehouse("1")
    doc = ReceivingDocument(number="CHIP-0001", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()

    html = client_logged_in.get(f"/receiving/{doc.id}").get_data(as_text=True)

    assert 'status-chip-draft status-chip-reached' in html
    assert 'status-chip-recounting status-chip-reached' not in html
    assert 'status-chip-sorting status-chip-reached' not in html
    assert 'status-chip-completed status-chip-reached' not in html


def test_recounting_document_shows_draft_and_recounting_reached(db, client_logged_in):
    wh = _make_warehouse("2")
    item = _make_item("2")
    doc = ReceivingDocument(number="CHIP-0002", warehouse_id=wh.id, invoice_file_name="накладная.xlsx")
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=5, expected_qty=5))
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/send-to-recount")

    html = client_logged_in.get(f"/receiving/{doc.id}").get_data(as_text=True)

    assert 'status-chip-draft status-chip-reached' in html
    assert 'status-chip-recounting status-chip-reached' in html
    assert 'status-chip-sorting status-chip-reached' not in html
    assert 'status-chip-completed status-chip-reached' not in html


def test_manual_receiving_completed_skips_recounting_and_sorting_chips(db, client_logged_in):
    """Ручная приемка (не из накладной) идет сразу draft -> completed —
    показывает только 2 плашки ("Приемка"/"Завершено", см.
    test_manual_receiving_shows_only_two_chips), плашек "Пересчет"/
    "Разбраковка" для нее вообще нет в разметке, а не просто непройдены."""
    wh = _make_warehouse("3")
    item = _make_item("3")
    doc = ReceivingDocument(number="CHIP-0003", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=5))
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/complete")

    html = client_logged_in.get(f"/receiving/{doc.id}").get_data(as_text=True)

    assert 'status-chip-draft status-chip-reached' in html
    assert 'status-chip-recounting' not in html
    assert 'status-chip-sorting' not in html
    assert 'status-chip-completed status-chip-reached' in html


def test_manual_receiving_shows_only_two_chips_labeled_priemka(db, client_logged_in):
    """Для ручной приемки (не из накладной) вместо 4 плашек (Ч/П/Р/З) —
    только 2: "Приемка" (переименованная первая плашка) и "Завершено"."""
    wh = _make_warehouse("5")
    doc = ReceivingDocument(number="CHIP-0005", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()

    html = client_logged_in.get(f"/receiving/{doc.id}").get_data(as_text=True)

    assert "Приемка:" in html
    assert ">Пр</span>" in html
    assert ">Ч</span>" not in html


def test_invoice_receiving_keeps_four_chips_labeled_chernovik(db, client_logged_in):
    """Приемка из накладной по-прежнему показывает все 4 плашки с исходными
    подписями (Ч/П/Р/З) — сокращение до 2 касается только ручной приемки."""
    wh = _make_warehouse("6")
    doc = ReceivingDocument(number="CHIP-0006", warehouse_id=wh.id, invoice_file_name="накладная.xlsx")
    db.session.add(doc)
    db.session.commit()

    html = client_logged_in.get(f"/receiving/{doc.id}").get_data(as_text=True)

    assert "Черновик:" in html
    assert ">Ч</span>" in html
    assert ">Пр</span>" not in html


def test_list_page_shows_status_chips(db, client_logged_in):
    wh = _make_warehouse("4")
    doc = ReceivingDocument(number="CHIP-0004", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()

    html = client_logged_in.get("/receiving/").get_data(as_text=True)

    assert "status-chips" in html
    assert 'status-chip-draft status-chip-reached' in html
