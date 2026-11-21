"""Загрузка плана отгрузок должна находить номенклатуру не только по
ОСНОВНОМУ штрихкоду, но и по ДОП. штрихкоду (Nomenclature.barcode2, см.
чат: товар "2012962030009" числился доп. штрихкодом у номенклатуры с
другим основным штрихкодом — план создавал строку с nomenclature_id=None,
и реальный остаток на складе не подтягивался в "Что нужно отправить")."""

import io

import openpyxl

from wms.extensions import db
from wms.models import Nomenclature, ShipmentPlanLine


def _plan_file(sheet_name, city, barcode, qty):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet(sheet_name)
    ws.append(["Артикул", "Размер", "Баркод", city])
    ws.append(["ART-1", "44", barcode, qty])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _upload(client, sheet_name, city, barcode, qty):
    return client.post(
        "/shipment-plan/upload",
        data={"file": (_plan_file(sheet_name, city, barcode, qty), "plan.xlsx")},
        content_type="multipart/form-data",
    )


def test_plan_barcode_matches_nomenclature_by_secondary_barcode(db, client_logged_in):
    item = Nomenclature(
        sku="SKU-B2-1", barcode="2041022609435", barcode2="2012962030009", name="Шапка", unit="шт",
    )
    db.session.add(item)
    db.session.commit()

    resp = _upload(client_logged_in, "Распределение ОЗОН ФБС от 01.09", "Москва", "2012962030009", 10)
    assert resp.status_code == 302

    line = ShipmentPlanLine.query.filter_by(barcode="2012962030009").first()
    assert line is not None
    assert line.nomenclature_id == item.id


def test_primary_barcode_still_wins_over_secondary_on_conflict(db, client_logged_in):
    """На случай редкого совпадения: если один и тот же штрихкод из плана
    одновременно является чьим-то ОСНОВНЫМ и чьим-то ДОП. штрихкодом,
    побеждает совпадение по основному — он однозначнее."""
    main_match = Nomenclature(sku="SKU-B2-MAIN", barcode="3330000000001", name="Основной матч", unit="шт")
    secondary_match = Nomenclature(
        sku="SKU-B2-SECOND", barcode="9990000000009", barcode2="3330000000001", name="Доп матч", unit="шт",
    )
    db.session.add_all([main_match, secondary_match])
    db.session.commit()

    _upload(client_logged_in, "Распределение ОЗОН ФБС от 01.09", "Москва", "3330000000001", 5)

    line = ShipmentPlanLine.query.filter_by(barcode="3330000000001").first()
    assert line is not None
    assert line.nomenclature_id == main_match.id


def test_plan_barcode_still_unmatched_when_not_found_anywhere(db, client_logged_in):
    resp = _upload(client_logged_in, "Распределение ОЗОН ФБС от 01.09", "Москва", "0000000000000", 7)
    assert resp.status_code == 302

    line = ShipmentPlanLine.query.filter_by(barcode="0000000000000").first()
    assert line is not None
    assert line.nomenclature_id is None
