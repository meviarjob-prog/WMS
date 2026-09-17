from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    InventoryDocument,
    MovementDocument,
    MovementLine,
    Nomenclature,
    PlacementDocument,
    ReceivingDocument,
    ReceivingLine,
    User,
    Warehouse,
)


def _user(username):
    user = User(username=username, role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def test_staff_sees_only_own_documents_and_cannot_open_foreign(db, client):
    first = _user("documents-first")
    second = _user("documents-second")
    warehouse = Warehouse(code="WH-DOC", name="Основной")
    db.session.add(warehouse)
    db.session.commit()

    definitions = (
        (ReceivingDocument, "/receiving/", "/receiving/{id}"),
        (PlacementDocument, "/placement/", "/placement/{id}"),
        (InventoryDocument, "/inventory/", "/inventory/{id}"),
    )
    created = []
    for index, (model, list_path, detail_path) in enumerate(definitions, start=1):
        own = model(number=f"OWN-{index}", warehouse_id=warehouse.id, created_by_id=first.id)
        foreign = model(number=f"FOREIGN-{index}", warehouse_id=warehouse.id, created_by_id=second.id)
        db.session.add_all([own, foreign])
        created.append((own, foreign, list_path, detail_path))

    db.session.commit()

    _login(client, first)
    for own, foreign, list_path, detail_path in created:
        html = client.get(list_path).get_data(as_text=True)
        assert own.number in html
        assert foreign.number not in html
        assert client.get(detail_path.format(id=foreign.id)).status_code == 404


def test_staff_sees_foreign_movement_only_while_draft(db, client):
    """Перемещение — особый случай среди документов: пока оно "черновик",
    его могут собирать сообща несколько сотрудников (см.
    movement.route_box_add — один и тот же маршрут ищется без учета
    автора, чтобы разные сотрудники, сканирующие короба на одно
    направление, попадали в один документ), поэтому черновик виден и
    находится любым сотрудником, а не только автором — иначе для того,
    кто добавил короб не первым, документ выглядел бы так, будто короб
    "потерялся". После завершения документ снова приватен, как и другие
    типы документов выше."""
    first = _user("movement-doc-first")
    second = _user("movement-doc-second")
    warehouse = Warehouse(code="WH-MOVE-DOC", name="Основной")
    target = Warehouse(code="WH-MOVE-DOC-2", name="Склад №2")
    db.session.add_all([warehouse, target])
    db.session.commit()

    own_draft = MovementDocument(
        number="OWN-MOVE", from_warehouse_id=warehouse.id, to_warehouse_id=target.id, created_by_id=first.id
    )
    foreign_draft = MovementDocument(
        number="FOREIGN-MOVE-DRAFT",
        from_warehouse_id=warehouse.id,
        to_warehouse_id=target.id,
        created_by_id=second.id,
    )
    foreign_completed = MovementDocument(
        number="FOREIGN-MOVE-DONE",
        from_warehouse_id=warehouse.id,
        to_warehouse_id=target.id,
        created_by_id=second.id,
        status="completed",
    )
    db.session.add_all([own_draft, foreign_draft, foreign_completed])
    db.session.commit()

    _login(client, first)
    html = client.get("/movement/").get_data(as_text=True)
    assert own_draft.number in html
    assert foreign_draft.number in html
    assert foreign_completed.number not in html

    assert client.get(f"/movement/{foreign_draft.id}").status_code == 200
    assert client.get(f"/movement/{foreign_completed.id}").status_code == 404
    # Видеть черновик — не значит мочь его завершить/изменить: это
    # по-прежнему только автор или админ (см. movement._restrict_document_access).
    assert client.post(f"/movement/{foreign_draft.id}/complete").status_code == 404


def test_invoice_receiving_view_permission_shows_foreign_invoice_read_only(db, client):
    receiver = _user("invoice-receiver")
    author = _user("invoice-author")
    receiver.invoice_receiving_view_allowed = True
    warehouse = Warehouse(code="WH-INVOICE-VIEW", name="Основной")
    db.session.add(warehouse)
    db.session.commit()
    invoice = ReceivingDocument(
        number="INVOICE-SHARED",
        warehouse_id=warehouse.id,
        created_by_id=author.id,
        invoice_file_name="накладная.xlsx",
    )
    manual = ReceivingDocument(
        number="MANUAL-PRIVATE",
        warehouse_id=warehouse.id,
        created_by_id=author.id,
    )
    db.session.add_all([invoice, manual])
    db.session.commit()
    item = Nomenclature(sku="SKU-INV-VIEW", barcode="77706000001", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=invoice.id, nomenclature_id=item.id, qty=5))
    db.session.commit()

    _login(client, receiver)
    html = client.get("/receiving/").get_data(as_text=True)
    assert invoice.number in html
    assert manual.number not in html
    assert client.get(f"/receiving/{invoice.id}").status_code == 200
    assert client.get(f"/receiving/{manual.id}").status_code == 404
    # Право "видит все приемки по накладным" дается приемщику/зав. складом
    # именно чтобы они ВЕЛИ чужие приемки по накладным целиком, а не только
    # смотрели — иначе, например, "Отправить на пересчет" падало бы в 404
    # для всех, кроме автора и админа (баг, а не намеренное ограничение).
    resp = client.post(f"/receiving/{invoice.id}/send-to-recount", follow_redirects=True)
    assert resp.status_code == 200
    assert ReceivingDocument.query.get(invoice.id).status == "recounting"
    # Приемка без загруженной накладной (manual) по-прежнему недоступна.
    assert client.post(f"/receiving/{manual.id}/send-to-recount").status_code == 404


def test_movement_view_permission_shows_foreign_movements_read_only(db, client):
    viewer = _user("movement-viewer")
    author = _user("movement-author")
    viewer.movement_view_allowed = True
    warehouse = Warehouse(code="WH-MOVE-VIEW", name="Основной")
    target = Warehouse(code="WH-MOVE-TARGET", name="Склад №2")
    db.session.add_all([warehouse, target])
    db.session.commit()
    movement = MovementDocument(
        number="MOVE-SHARED",
        from_warehouse_id=warehouse.id,
        to_warehouse_id=target.id,
        created_by_id=author.id,
    )
    db.session.add(movement)
    db.session.commit()

    _login(client, viewer)
    html = client.get("/movement/").get_data(as_text=True)
    assert movement.number in html
    detail = client.get(f"/movement/{movement.id}")
    assert detail.status_code == 200
    assert "📦 Собрано" not in detail.get_data(as_text=True)
    assert client.post(f"/movement/{movement.id}/complete").status_code == 404


def test_non_admin_viewer_can_toggle_marketplace_bookkeeping_marks_on_foreign_movement(db, client):
    """Баг: галочки "внесено в 1С"/"заявка на МП создана" и номер заявки —
    это просто пометки для контроля (см. movement.toggle_accounting/
    toggle_marketplace_request/update_marketplace_request_number), они не
    меняют сам документ. Но общий before_request раньше требовал
    авторства/админства для ЛЮБОГО изменяющего маршрута с doc_id, поэтому
    пользователь с правом "видит все перемещения" (не автор, не админ)
    получал 404 при попытке их проставить на чужом документе."""
    viewer = _user("movement-bookkeeper")
    author = _user("movement-bookkeeping-author")
    viewer.movement_view_allowed = True
    warehouse = Warehouse(code="WH-MOVE-BOOK-1", name="Основной")
    target = Warehouse(code="WH-MOVE-BOOK-2", name="Склад №2")
    db.session.add_all([warehouse, target])
    db.session.commit()
    movement = MovementDocument(
        number="MOVE-BOOKKEEPING",
        from_warehouse_id=warehouse.id,
        to_warehouse_id=target.id,
        created_by_id=author.id,
        status="completed",
    )
    db.session.add(movement)
    db.session.commit()

    _login(client, viewer)

    resp = client.post(f"/movement/{movement.id}/toggle-accounting", follow_redirects=True)
    assert resp.status_code == 200
    assert MovementDocument.query.get(movement.id).accounting_entered_at is not None

    resp = client.post(f"/movement/{movement.id}/toggle-marketplace-request", follow_redirects=True)
    assert resp.status_code == 200
    assert MovementDocument.query.get(movement.id).marketplace_request_created_at is not None

    resp = client.post(
        f"/movement/{movement.id}/marketplace-request-number",
        data={"marketplace_request_number": "REQ-999"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert MovementDocument.query.get(movement.id).marketplace_request_number == "REQ-999"

    # Реальное изменение документа (не просто пометка) чужим не-админом
    # по-прежнему запрещено.
    assert client.post(f"/movement/{movement.id}/complete").status_code == 404


def test_receiving_offers_only_main_and_second_warehouse(db, client_logged_in):
    main = Warehouse(code="WH-001", name="Основной")
    second = Warehouse(code="WH-002", name="Склад №2")
    shosseynaya = Warehouse(code="WH-002-A", name="Склад №2 (Шоссейная 167)")
    hidden = Warehouse(code="WH-003", name="Транзитный")
    db.session.add_all([main, second, shosseynaya, hidden])
    db.session.commit()

    html = client_logged_in.get("/receiving/new").get_data(as_text=True)
    assert "Основной" in html
    assert "Склад №2" in html
    assert "Склад №2 (Шоссейная 167)" in html
    assert "Транзитный" not in html

    client_logged_in.post("/receiving/new", data={"warehouse_id": hidden.id})
    assert ReceivingDocument.query.filter_by(warehouse_id=hidden.id).count() == 0


def test_warehouse_mapping_requires_separate_permission(db, client):
    worker = _user("mapping-worker")
    city = Warehouse(code="CITY-1", name="ОЗОН Казань", marketplace="ozon")
    db.session.add(city)
    db.session.commit()
    _login(client, worker)

    client.post(
        f"/warehouses/{city.id}/fulfillment-1c-name",
        data={"fulfillment_1c_name": "Склад 1С"},
    )
    assert Warehouse.query.get(city.id).fulfillment_1c_name is None

    worker.warehouse_mapping_allowed = True
    db.session.commit()
    client.post(
        f"/warehouses/{city.id}/fulfillment-1c-name",
        data={"fulfillment_1c_name": "Склад 1С"},
    )
    assert Warehouse.query.get(city.id).fulfillment_1c_name == "Склад 1С"


def test_admin_can_grant_warehouse_mapping_permission_in_user_settings(
    db, client_logged_in
):
    worker = _user("mapping-settings-worker")
    html = client_logged_in.get("/users").get_data(as_text=True)
    assert "Разрешить сопоставление складов с 1С" in html

    client_logged_in.post(
        f"/users/{worker.id}/sections",
        data={
            "mode": "full",
            "nomenclature_edit": "on",
            "warehouse_mapping": "on",
            "movement_view": "on",
            "movement_complete": "on",
        },
    )
    assert User.query.get(worker.id).warehouse_mapping_allowed is True
    assert User.query.get(worker.id).movement_view_allowed is True
    assert User.query.get(worker.id).movement_complete_allowed is True

    # Не отмечена — снимается (а не остается как было), как и остальные
    # галочки этой формы.
    client_logged_in.post(
        f"/users/{worker.id}/sections",
        data={"mode": "full"},
    )
    assert User.query.get(worker.id).movement_complete_allowed is False


def test_admin_can_toggle_admin_rights_for_other_user(db, client_logged_in):
    worker = _user("future-admin")
    assert worker.is_admin is False

    resp = client_logged_in.post(f"/users/{worker.id}/toggle-admin", follow_redirects=True)
    assert resp.status_code == 200
    assert User.query.get(worker.id).is_admin is True

    resp = client_logged_in.post(f"/users/{worker.id}/toggle-admin", follow_redirects=True)
    assert resp.status_code == 200
    assert User.query.get(worker.id).is_admin is False


def test_admin_cannot_toggle_own_admin_rights(db, client_logged_in, admin_user):
    resp = client_logged_in.post(f"/users/{admin_user.id}/toggle-admin", follow_redirects=True)
    assert resp.status_code == 200
    assert User.query.get(admin_user.id).is_admin is True


def test_movement_complete_permission_allows_completing_foreign_movement(db, client):
    """Право "завершение чужих перемещений" (User.movement_complete_allowed)
    дает те же кнопки, что автору/админу — завершить, принять на складе,
    принять с расхождением — но не более того (удалить документ по-прежнему
    только админ)."""
    manager = _user("movement-completer")
    author = _user("movement-completer-author")
    manager.movement_complete_allowed = True
    warehouse = Warehouse(code="WH-MOVE-COMPLETE-1", name="Основной")
    target = Warehouse(code="WH-MOVE-COMPLETE-2", name="Склад №2")
    db.session.add_all([warehouse, target])
    db.session.commit()

    box = Box(box_number="BOX-MOVE-COMPLETE-1", warehouse_id=warehouse.id, status="open")
    db.session.add(box)
    db.session.commit()
    item = Nomenclature(sku="SKU-MOVE-COMPLETE", barcode="77709000001", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=2))

    movement = MovementDocument(
        number="MOVE-COMPLETE-1",
        from_warehouse_id=warehouse.id,
        to_warehouse_id=target.id,
        created_by_id=author.id,
    )
    db.session.add(movement)
    db.session.commit()
    db.session.add(MovementLine(document_id=movement.id, box_id=box.id, from_warehouse_id=warehouse.id))
    db.session.commit()

    _login(client, manager)

    detail_html = client.get(f"/movement/{movement.id}").get_data(as_text=True)
    assert "📦 Собрано" in detail_html

    resp = client.post(f"/movement/{movement.id}/complete", follow_redirects=True)
    assert resp.status_code == 200
    assert MovementDocument.query.get(movement.id).status == "completed"

    client.post(f"/movement/{movement.id}/mark-marketplace-request")

    resp = client.post(f"/movement/{movement.id}/receive", follow_redirects=True)
    assert resp.status_code == 200
    assert MovementDocument.query.get(movement.id).received_at is not None

    # Удаление документа этим правом не разрешается — только админ.
    assert client.post(f"/movement/{movement.id}/delete").status_code == 404


def test_box_transfer_is_available_in_movements_and_shows_contents(db, client_logged_in):
    warehouse = Warehouse(code="WH-BOX-TRANSFER", name="Основной")
    item = Nomenclature(sku="TRANSFER-SKU", barcode="9900001", name="Товар в коробе", unit="шт")
    source = Box(box_number="BOX-TRANSFER-S", warehouse=warehouse)
    target = Box(box_number="BOX-TRANSFER-T", warehouse=warehouse)
    db.session.add_all([warehouse, item, source, target])
    db.session.commit()
    source_item = BoxItem(box_id=source.id, nomenclature_id=item.id, qty=5)
    db.session.add(source_item)
    db.session.commit()

    html = client_logged_in.get(
        "/movement/box-transfer", query_string={"source_box_number": source.box_number}
    ).get_data(as_text=True)
    assert "Товар в коробе" in html

    client_logged_in.post(
        f"/movement/box-transfer/items/{source_item.id}",
        data={"source_box_id": source.id, "target_box_number": target.box_number, "qty": 2},
    )
    assert BoxItem.query.filter_by(box_id=source.id, nomenclature_id=item.id).first().qty == 3
    assert BoxItem.query.filter_by(box_id=target.id, nomenclature_id=item.id).first().qty == 2
