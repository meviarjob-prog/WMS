"""Сличительная ведомость в инвентаризации: учётный остаток склада
(неразмещенный остаток + товар в коробах на этом складе) против того, что
реально насчитали в листе — с расхождением (излишек/недостача) по каждой
позиции, включая товар, который есть на складе, но не попал в подсчет, и
наоборот."""

from wms.extensions import db
from wms.models import Box, BoxItem, InventoryDocument, Nomenclature, UnplacedStock, Warehouse


def _make_warehouse(code="WH-CMP"):
    wh = Warehouse(code=code, name="Тест склад сверки")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(barcode, name="Товар сверки"):
    item = Nomenclature(sku=barcode, barcode=barcode, name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _new_inventory(client, warehouse):
    client.post("/inventory/new", data={"warehouse_id": warehouse.id})
    return InventoryDocument.query.filter_by(warehouse_id=warehouse.id).order_by(
        InventoryDocument.id.desc()
    ).first()


def _scan_box(client, doc, box):
    client.post(f"/inventory/{doc.id}/boxes/add", data={"box_number": box.box_number})


def _add_line(client, doc, item, qty):
    client.post(
        f"/inventory/{doc.id}/lines/add",
        data={"nomenclature_id": item.id, "qty": qty},
    )


def _row_html(html, needle):
    """Строка <tr>...</tr> сличительной ведомости (не путать со "Списком
    товара" слева, где та же номенклатура тоже упоминается) — data-diff
    стоит в открывающем теге, то есть раньше самого текста в HTML, поэтому
    просто искать вперед от needle недостаточно."""
    table_start = html.index('id="comparisonTable"')
    idx = html.index(needle, table_start)
    row_start = html.rfind("<tr", 0, idx)
    row_end = html.find("</tr>", idx)
    return html[row_start:row_end]


def test_comparison_flags_shortage_and_surplus(db, client_logged_in):
    """Расхождения считаются через ручной подсчет "без короба" (add_line),
    т.к. для товара, попавшего в подсчет через сам короб, посчитанное
    количество берется из содержимого этого же короба, которое также
    формирует и учётный остаток — расхождение для него структурно всегда 0."""
    wh = _make_warehouse("WH-CMP-1")
    short_item = _make_item("8880000201", "Товар с недостачей")
    surplus_item = _make_item("8880000202", "Товар с излишком")
    match_item = _make_item("8880000203", "Товар без расхождений")

    # По учету: недостачи товара 10, совпадающего 5; товара-излишка на
    # складе по учету нет вовсе.
    UnplacedStock.add(wh.id, short_item.id, 10)
    UnplacedStock.add(wh.id, match_item.id, 5)
    db.session.commit()

    doc = _new_inventory(client_logged_in, wh)
    _add_line(client_logged_in, doc, short_item, 7)  # насчитали 7 из 10 -> недостача 3
    _add_line(client_logged_in, doc, surplus_item, 4)  # по учету 0 -> излишек 4
    _add_line(client_logged_in, doc, match_item, 5)  # сходится

    html = client_logged_in.get(f"/inventory/{doc.id}").get_data(as_text=True)

    assert 'data-diff="-3' in _row_html(html, "Товар с недостачей")
    assert 'data-diff="4' in _row_html(html, "Товар с излишком")
    assert 'data-diff="0' in _row_html(html, "Товар без расхождений")


def test_comparison_boxed_item_is_never_a_diff_by_itself(db, client_logged_in):
    """Товар, посчитанный через скан короба, не даёт расхождения сам по
    себе — короб одновременно и формирует учётный остаток (see
    _warehouse_stock_by_nomenclature), и подсчитанное количество. Это
    задокументированное поведение, а не баг: для настоящего расхождения по
    такому товару нужен либо неучтенный (несосканированный) короб, либо
    ручная правка количества через update_line."""
    wh = _make_warehouse("WH-CMP-5")
    item = _make_item("8880000206", "Товар из короба")
    box = Box(box_number="BOX-CMP-5", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=6))
    db.session.commit()

    doc = _new_inventory(client_logged_in, wh)
    _scan_box(client_logged_in, doc, box)

    html = client_logged_in.get(f"/inventory/{doc.id}").get_data(as_text=True)
    assert 'data-diff="0' in _row_html(html, "Товар из короба")


def test_comparison_shows_shortage_for_unscanned_existing_box(db, client_logged_in):
    """Короб есть на складе (значит числится в учётном остатке), но его не
    отсканировали в этом листе -> недостача по его содержимому."""
    wh = _make_warehouse("WH-CMP-6")
    item = _make_item("8880000207", "Товар в несосканированном коробе")
    box = Box(box_number="BOX-CMP-6", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=8))
    db.session.commit()

    doc = _new_inventory(client_logged_in, wh)  # короб намеренно не сканируем

    html = client_logged_in.get(f"/inventory/{doc.id}").get_data(as_text=True)
    assert 'data-diff="-8' in _row_html(html, "Товар в несосканированном коробе")


def test_comparison_includes_system_stock_never_counted(db, client_logged_in):
    """Товар, который есть на складе по учету, но не попал ни в один
    отсканированный короб инвентаризации, должен показаться как недостача
    (посчитано 0), а не молча пропасть из сверки."""
    wh = _make_warehouse("WH-CMP-2")
    item = _make_item("8880000204", "Забытый при подсчете")
    UnplacedStock.add(wh.id, item.id, 6)
    db.session.commit()

    doc = _new_inventory(client_logged_in, wh)

    html = client_logged_in.get(f"/inventory/{doc.id}").get_data(as_text=True)
    assert 'data-diff="-6' in _row_html(html, "Забытый при подсчете")


def test_comparison_scoped_to_document_warehouse_only(db, client_logged_in):
    """Остаток другого склада не должен подмешиваться в сверку."""
    wh1 = _make_warehouse("WH-CMP-3")
    wh2 = _make_warehouse("WH-CMP-4")
    item = _make_item("8880000205", "Товар на другом складе")
    UnplacedStock.add(wh2.id, item.id, 9)
    db.session.commit()

    doc = _new_inventory(client_logged_in, wh1)

    html = client_logged_in.get(f"/inventory/{doc.id}").get_data(as_text=True)
    assert "Товар на другом складе" not in html
