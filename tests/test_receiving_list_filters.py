"""Список приемок (receiving.list_documents) — галочки "показывать только
незавершенные" и "приемка по накладным". Вторая опирается на supplier_id:
он проставляется только при загрузке накладной (см.
receiving._find_or_create_supplier), у ручных приемок его нет.

Поиск (q) настроен так же, как в перемещениях (см. чат и
test_movement_pagination.py) — несколько слов объединяются через И, но
каждое слово может совпасть в любом из полей (номер, поставщик, № заказа,
склад, автор), а не обязательно в одном и том же."""

from wms.extensions import db
from wms.models import ReceivingDocument, Supplier, User, Warehouse


def _make_warehouse(code="WH-RL"):
    wh = Warehouse(code=code, name="Тест склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def test_unfinished_filter_hides_completed_documents(db, client_logged_in):
    wh = _make_warehouse()
    draft = ReceivingDocument(number="RL-1", warehouse_id=wh.id, status="draft")
    completed = ReceivingDocument(number="RL-2", warehouse_id=wh.id, status="completed")
    db.session.add_all([draft, completed])
    db.session.commit()

    html = client_logged_in.get("/receiving/?unfinished=on").get_data(as_text=True)

    assert "RL-1" in html
    assert "RL-2" not in html


def test_without_filter_shows_all_documents(db, client_logged_in):
    wh = _make_warehouse()
    draft = ReceivingDocument(number="RL-3", warehouse_id=wh.id, status="draft")
    completed = ReceivingDocument(number="RL-4", warehouse_id=wh.id, status="completed")
    db.session.add_all([draft, completed])
    db.session.commit()

    html = client_logged_in.get("/receiving/").get_data(as_text=True)

    assert "RL-3" in html
    assert "RL-4" in html


def test_invoice_only_filter_keeps_documents_with_supplier_id(db, client_logged_in):
    from wms.models import Supplier

    wh = _make_warehouse()
    supplier = Supplier(name="ООО Тест")
    db.session.add(supplier)
    db.session.commit()

    manual = ReceivingDocument(number="RL-5", warehouse_id=wh.id, supplier="Просто текст")
    from_invoice = ReceivingDocument(number="RL-6", warehouse_id=wh.id, supplier_id=supplier.id)
    db.session.add_all([manual, from_invoice])
    db.session.commit()

    html = client_logged_in.get("/receiving/?invoice_only=on").get_data(as_text=True)

    assert "RL-5" not in html
    assert "RL-6" in html


def test_search_matches_supplier_by_substring(db, client_logged_in):
    wh = _make_warehouse("WH-RL-SUP")
    matching = ReceivingDocument(number="RL-7", warehouse_id=wh.id, supplier="ИП Иванов Иван Иванович")
    other = ReceivingDocument(number="RL-8", warehouse_id=wh.id, supplier="ООО Ромашка")
    db.session.add_all([matching, other])
    db.session.commit()

    html = client_logged_in.get("/receiving/?q=Иванов").get_data(as_text=True)

    assert "RL-7" in html
    assert "RL-8" not in html


def test_search_matches_document_number(db, client_logged_in):
    wh = _make_warehouse("WH-RL-NUM")
    matching = ReceivingDocument(number="RL-NUM-777", warehouse_id=wh.id)
    other = ReceivingDocument(number="RL-NUM-888", warehouse_id=wh.id)
    db.session.add_all([matching, other])
    db.session.commit()

    html = client_logged_in.get("/receiving/?q=777").get_data(as_text=True)

    assert "RL-NUM-777" in html
    assert "RL-NUM-888" not in html


def test_search_matches_order_number(db, client_logged_in):
    wh = _make_warehouse("WH-RL-ORD")
    matching = ReceivingDocument(number="RL-11", warehouse_id=wh.id, order_number="ЗАЯВ-42")
    other = ReceivingDocument(number="RL-12", warehouse_id=wh.id, order_number="ЗАЯВ-99")
    db.session.add_all([matching, other])
    db.session.commit()

    html = client_logged_in.get("/receiving/?q=ЗАЯВ-42").get_data(as_text=True)

    assert "RL-11" in html
    assert "RL-12" not in html


def test_search_matches_warehouse_name(db, client_logged_in):
    wh1 = _make_warehouse("WH-RL-SN1")
    wh1.name = "Москва основной"
    wh2 = _make_warehouse("WH-RL-SN2")
    db.session.commit()
    doc1 = ReceivingDocument(number="RL-13", warehouse_id=wh1.id)
    doc2 = ReceivingDocument(number="RL-14", warehouse_id=wh2.id)
    db.session.add_all([doc1, doc2])
    db.session.commit()

    html = client_logged_in.get("/receiving/?q=Москва").get_data(as_text=True)

    assert "RL-13" in html
    assert "RL-14" not in html


def test_search_matches_supplier_from_directory(db, client_logged_in):
    """supplier_ref (справочник поставщиков, см. _find_or_create_supplier)
    ищется отдельно от свободного текстового поля supplier — по имени и
    ИНН."""
    wh = _make_warehouse("WH-RL-SUPREF")
    supplier = Supplier(name="ООО Ромашка Опт", inn="7712345678")
    db.session.add(supplier)
    db.session.commit()
    matching = ReceivingDocument(number="RL-15", warehouse_id=wh.id, supplier_id=supplier.id)
    other = ReceivingDocument(number="RL-16", warehouse_id=wh.id, supplier="ИП Другой")
    db.session.add_all([matching, other])
    db.session.commit()

    html = client_logged_in.get("/receiving/?q=7712345678").get_data(as_text=True)

    assert "RL-15" in html
    assert "RL-16" not in html


def test_search_matches_author(db, client_logged_in):
    wh = _make_warehouse("WH-RL-AUT")
    author = User(username="warehouse-kladovshik", full_name="Иван Складовщиков", role="warehouse")
    author.set_password("x")
    db.session.add(author)
    db.session.commit()
    matching = ReceivingDocument(number="RL-17", warehouse_id=wh.id, created_by_id=author.id)
    other = ReceivingDocument(number="RL-18", warehouse_id=wh.id)
    db.session.add_all([matching, other])
    db.session.commit()

    html = client_logged_in.get("/receiving/?q=Складовщиков").get_data(as_text=True)

    assert "RL-17" in html
    assert "RL-18" not in html


def test_search_with_two_words_matches_across_different_fields(db, client_logged_in):
    """«Иванов Москва» — оба слова должны совпасть, но не обязательно в
    одном и том же поле: "Иванов" в поставщике, "Москва" в складе (см. чат,
    та же логика, что и в поиске по перемещениям)."""
    wh_moscow = _make_warehouse("WH-RL-2W1")
    wh_moscow.name = "Москва основной"
    wh_other = _make_warehouse("WH-RL-2W2")
    db.session.commit()
    matching = ReceivingDocument(number="RL-19", warehouse_id=wh_moscow.id, supplier="ИП Иванов")
    other_supplier = ReceivingDocument(number="RL-20", warehouse_id=wh_moscow.id, supplier="ИП Петров")
    other_warehouse = ReceivingDocument(number="RL-21", warehouse_id=wh_other.id, supplier="ИП Иванов")
    db.session.add_all([matching, other_supplier, other_warehouse])
    db.session.commit()

    html = client_logged_in.get("/receiving/?q=Иванов+Москва").get_data(as_text=True)

    assert "RL-19" in html
    assert "RL-20" not in html
    assert "RL-21" not in html


def test_search_no_match_shows_empty_list(db, client_logged_in):
    wh = _make_warehouse("WH-RL-EMPTY")
    doc = ReceivingDocument(number="RL-22", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()

    html = client_logged_in.get("/receiving/?q=NOTHING-HERE").get_data(as_text=True)

    assert "RL-22" not in html


def test_list_page_has_search_field(db, client_logged_in):
    html = client_logged_in.get("/receiving/").get_data(as_text=True)
    assert 'name="q"' in html


def test_warehouse_filter_keeps_only_matching_warehouse(db, client_logged_in):
    wh1 = _make_warehouse("WH-RL-A")
    wh2 = _make_warehouse("WH-RL-B")
    doc1 = ReceivingDocument(number="RL-9", warehouse_id=wh1.id)
    doc2 = ReceivingDocument(number="RL-10", warehouse_id=wh2.id)
    db.session.add_all([doc1, doc2])
    db.session.commit()

    html = client_logged_in.get(f"/receiving/?warehouse_id={wh1.id}").get_data(as_text=True)

    assert "RL-9" in html
    assert "RL-10" not in html
