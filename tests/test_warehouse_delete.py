from wms.extensions import db
from wms.models import Box, ShipmentPlan, ShipmentPlanLine, User, Warehouse


def _marketplace_warehouse(code="WH-DELETE", city="Ошибочный город"):
    warehouse = Warehouse(
        code=code,
        name=city,
        marketplace="ozon",
        marketplace_city=city,
    )
    db.session.add(warehouse)
    db.session.commit()
    return warehouse


def _login(client, user):
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def test_admin_can_delete_imported_warehouse_and_its_plan_lines(db, client_logged_in):
    warehouse = _marketplace_warehouse()
    plan = ShipmentPlan(marketplace="ozon", sheet_name="Ошибочное распределение")
    db.session.add(plan)
    db.session.flush()
    db.session.add(
        ShipmentPlanLine(
            plan_id=plan.id,
            warehouse_id=warehouse.id,
            barcode="4600000000001",
            planned_qty=25,
        )
    )
    db.session.commit()

    response = client_logged_in.post(
        f"/warehouses/{warehouse.id}/delete", follow_redirects=True
    )

    assert response.status_code == 200
    assert Warehouse.query.get(warehouse.id) is None
    assert ShipmentPlanLine.query.filter_by(warehouse_id=warehouse.id).count() == 0
    assert "Лишний склад" in response.get_data(as_text=True)


def test_imported_warehouse_with_operational_data_cannot_be_deleted(db, client_logged_in):
    warehouse = _marketplace_warehouse("WH-DELETE-BUSY", "Город с коробом")
    box = Box(box_number="BOX-WH-DELETE", warehouse_id=warehouse.id)
    db.session.add(box)
    db.session.commit()

    response = client_logged_in.post(
        f"/warehouses/{warehouse.id}/delete", follow_redirects=True
    )

    assert response.status_code == 200
    assert Warehouse.query.get(warehouse.id) is not None
    assert Box.query.get(box.id) is not None
    html = response.get_data(as_text=True)
    assert "нельзя удалить" in html
    assert "короба: 1" in html


def test_regular_warehouse_cannot_be_deleted(db, client_logged_in):
    warehouse = Warehouse(code="WH-DELETE-REG", name="Обычный склад")
    db.session.add(warehouse)
    db.session.commit()

    response = client_logged_in.post(
        f"/warehouses/{warehouse.id}/delete", follow_redirects=True
    )

    assert response.status_code == 200
    assert Warehouse.query.get(warehouse.id) is not None
    assert "Обычный склад нельзя удалить" in response.get_data(as_text=True)


def test_non_admin_cannot_delete_imported_warehouse(db, client):
    warehouse = _marketplace_warehouse("WH-DELETE-RIGHTS", "Чужой город")
    user = User(username="warehouse-delete-worker", role="warehouse")
    user.set_password("test")
    db.session.add(user)
    db.session.commit()
    _login(client, user)

    response = client.post(
        f"/warehouses/{warehouse.id}/delete", follow_redirects=True
    )

    assert response.status_code == 200
    assert Warehouse.query.get(warehouse.id) is not None
    assert "только администратор" in response.get_data(as_text=True)
