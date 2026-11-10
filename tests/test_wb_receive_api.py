"""Проверка приемки WB через API (supplies-api): итоги по товарам,
расхождения по коробам, токен на странице «Обмен с WB»."""

import base64
import json
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from wms.extensions import db
from wms.models import AppSetting, Box, BoxItem, MovementDocument, MovementLine, Warehouse
from wms.utils import wb_api
from tests.test_movement_receive_review import _make_doc, _nomenclature


def _wb_world(suffix):
    sender = Warehouse(code=f"WH-WB{suffix}S", name=f"Основной {suffix}")
    dest = Warehouse(code=f"WH-WB{suffix}D", name=f"Коледино WB {suffix}", marketplace="wb")
    db.session.add_all([sender, dest])
    db.session.commit()
    db.session.add(AppSetting(key="wb_api_token", value="TOKEN-123"))
    db.session.commit()
    return sender, dest


def _goods(rows):
    return [{"barcode": nom.barcode, "quantity": sent, "acceptedQuantity": got} for nom, sent, got in rows]


def _patch_wb(details=None, goods=None, discrepancies=None, packages=None, supplies=None):
    return [
        patch("wms.blueprints.movement.fetch_wb_supply_details", return_value=details or {"statusID": 5}),
        patch("wms.blueprints.movement.fetch_wb_supply_goods", return_value=goods or []),
        patch("wms.blueprints.movement.fetch_wb_supply_discrepancies", return_value=discrepancies or []),
        patch("wms.blueprints.movement.fetch_wb_supply_packages", return_value=packages or []),
        patch("wms.blueprints.movement.fetch_wb_supplies", return_value=supplies or []),
    ]


class _Patches:
    def __init__(self, **kw):
        self.patches = _patch_wb(**kw)

    def __enter__(self):
        self.mocks = [p.start() for p in self.patches]
        return self.mocks

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


# ---- клиент API -------------------------------------------------------------

def _fast_limiter():
    return wb_api._Limiter(capacity=100, interval=0.001)


@patch("wms.utils.wb_api.requests.request")
def test_wb_request_sends_token_and_waits_ratelimit_retry(mock_req):
    mock_req.side_effect = [
        Mock(status_code=429, headers={"X-Ratelimit-Retry": "3"}, text=""),
        Mock(status_code=200, json=lambda: [{"supplyID": 1}]),
    ]
    waits = []
    data = wb_api.wb_request("TKN", "GET", "/api/v1/x", limiter=_fast_limiter(), _sleep=waits.append)
    assert data == [{"supplyID": 1}]
    assert mock_req.call_args.kwargs["headers"]["Authorization"] == "TKN"
    assert 3.0 in waits


@patch("wms.utils.wb_api.requests.request")
def test_wb_request_404_and_4xx_not_retried(mock_req):
    mock_req.return_value = Mock(status_code=404, headers={}, text="")
    with pytest.raises(wb_api.WbNotFound):
        wb_api.wb_request("TKN", "GET", "/x", limiter=_fast_limiter(), _sleep=lambda s: None)
    mock_req.reset_mock()
    mock_req.return_value = Mock(status_code=400, headers={}, text="bad")
    with pytest.raises(wb_api.WbApiError):
        wb_api.wb_request("TKN", "GET", "/x", limiter=_fast_limiter(), _sleep=lambda s: None)
    assert mock_req.call_count == 1      # 4xx стоит как 10 запросов — не повторяем


def test_wb_request_without_token():
    with pytest.raises(wb_api.WbApiError, match="Обмен с WB"):
        wb_api.wb_request(None, "GET", "/x")


# ---- массовая проверка ------------------------------------------------------

def test_wb_full_match_auto_accepted(db, client_logged_in):
    sender, dest = _wb_world(1)
    n1 = _nomenclature(101)
    doc, _ = _make_doc("PER-WB0001", "30000001", sender, dest, [[(n1, 10)]])
    with _Patches(goods=_goods([(n1, 10, 10)])):
        html = client_logged_in.post("/movement/receive-review/run", data={"doc_ids": [str(doc.id)]}).get_data(as_text=True)
    assert "совпало с маркетплейсом (1)" in html
    db.session.refresh(doc)
    assert doc.received_at is not None


def test_wb_not_accepted_yet_goes_to_manual(db, client_logged_in):
    sender, dest = _wb_world(2)
    n1 = _nomenclature(102)
    doc, _ = _make_doc("PER-WB0002", "30000002", sender, dest, [[(n1, 10)]])
    with _Patches(details={"statusID": 4}):
        html = client_logged_in.post("/movement/receive-review/run", data={"doc_ids": [str(doc.id)]}).get_data(as_text=True)
    assert "приемка на WB еще не завершена — статус «Идёт приёмка»" in html
    db.session.refresh(doc)
    assert doc.received_at is None


def test_wb_box_discrepancies_return_missing_box_and_fix_quantities(db, client_logged_in):
    sender, dest = _wb_world(3)
    n1 = _nomenclature(103, name="Шапка")
    n2 = _nomenclature(104, name="Шарф")
    doc, boxes = _make_doc("PER-WB0003", "30000003", sender, dest, [[(n1, 10)], [(n2, 4)], [(n1, 5)]])
    # Второй короб не доехал целиком, в первом не хватило 2 шапок.
    discrepancies = [
        {"packageCode": boxes[1].box_number, "items": [
            {"declaredSku": n2.barcode, "declaredAmount": 4, "actualSku": "", "actualAmount": 0, "discrepancyType": "shortage"}]},
        {"packageCode": boxes[0].box_number.lower(), "items": [
            {"declaredSku": n1.barcode, "declaredAmount": 10, "actualSku": n1.barcode, "actualAmount": 8, "discrepancyType": "shortage"}]},
    ]
    with _Patches(goods=_goods([(n1, 15, 13), (n2, 4, 0)]), discrepancies=discrepancies):
        html = client_logged_in.post("/movement/receive-review/run", data={"doc_ids": [str(doc.id)]}).get_data(as_text=True)
    assert "WB: исправлено по коробам (1)" in html and boxes[1].box_number in html
    db.session.expire_all()
    doc = MovementDocument.query.get(doc.id)
    assert doc.received_at is not None and doc.receipt_returned_boxes == boxes[1].box_number
    assert Box.query.get(boxes[1].id).warehouse_id == sender.id
    assert BoxItem.query.filter_by(box_id=boxes[0].id).one().qty == 8
    assert BoxItem.query.filter_by(box_id=boxes[2].id).one().qty == 5


def test_wb_own_package_codes_matched_by_composition(db, client_logged_in):
    sender, dest = _wb_world(4)
    n1 = _nomenclature(105)
    n2 = _nomenclature(106)
    doc, boxes = _make_doc("PER-WB0004", "30000004", sender, dest, [[(n1, 3)], [(n2, 7)]])
    discrepancies = [{"packageCode": "WB_2282893992", "items": [
        {"declaredSku": n2.barcode, "declaredAmount": 7, "actualSku": "", "actualAmount": 0}]}]
    packages = [
        {"packageCode": "WB_1111", "quantity": 3, "barcodes": [{"barcode": n1.barcode, "quantity": 3}]},
        {"packageCode": "WB_2282893992", "quantity": 7, "barcodes": [{"barcode": n2.barcode, "quantity": 7}]},
    ]
    with _Patches(goods=_goods([(n1, 3, 3), (n2, 7, 0)]), discrepancies=discrepancies, packages=packages):
        client_logged_in.post("/movement/receive-review/run", data={"doc_ids": [str(doc.id)]})
    db.session.expire_all()
    assert MovementDocument.query.get(doc.id).receipt_returned_boxes == boxes[1].box_number


def test_wb_inconsistent_box_data_goes_to_manual(db, client_logged_in):
    sender, dest = _wb_world(5)
    n1 = _nomenclature(107)
    doc, boxes = _make_doc("PER-WB0005", "30000005", sender, dest, [[(n1, 10)], [(n1, 10)]])
    # По итогам недостача 5, а расхождения по коробам говорят о 3 — данные не сходятся.
    discrepancies = [{"packageCode": boxes[0].box_number, "items": [
        {"declaredSku": n1.barcode, "declaredAmount": 10, "actualSku": n1.barcode, "actualAmount": 7}]}]
    with _Patches(goods=_goods([(n1, 20, 15)]), discrepancies=discrepancies):
        html = client_logged_in.post("/movement/receive-review/run", data={"doc_ids": [str(doc.id)]}).get_data(as_text=True)
    assert "Требуется ручная проверка (1)" in html and "по коробам WB разобрать не удалось" in html
    db.session.refresh(doc)
    assert doc.received_at is None


def test_wb_preorder_number_resolved_to_supply(db, client_logged_in):
    from wms.utils.wb_api import WbNotFound

    sender, dest = _wb_world(6)
    n1 = _nomenclature(108)
    doc, _ = _make_doc("PER-WB0006", "555", sender, dest, [[(n1, 2)]])
    patches = _patch_wb(goods=_goods([(n1, 2, 2)]), supplies=[{"preorderID": 555, "supplyID": 777, "statusID": 5}])
    mocks = [p.start() for p in patches]
    try:
        mocks[0].side_effect = [WbNotFound("404"), {"statusID": 5}]
        client_logged_in.post("/movement/receive-review/run", data={"doc_ids": [str(doc.id)]})
        assert mocks[0].call_args_list[1].kwargs.get("preorder") is True
        assert mocks[1].call_args.kwargs.get("preorder") is True
    finally:
        for p in patches:
            p.stop()
    db.session.refresh(doc)
    assert doc.received_at is not None


# ---- одно перемещение -------------------------------------------------------

def test_wb_single_doc_shows_act_reconciliation_and_confirm_returns_box(db, client_logged_in):
    sender, dest = _wb_world(7)
    n1 = _nomenclature(109)
    doc, boxes = _make_doc("PER-WB0007", "30000007", sender, dest, [[(n1, 6)], [(n1, 4)]])
    discrepancies = [{"packageCode": boxes[1].box_number, "items": [
        {"declaredSku": n1.barcode, "declaredAmount": 4, "actualSku": "", "actualAmount": 0}]}]
    with _Patches(goods=_goods([(n1, 10, 6)]), discrepancies=discrepancies):
        html = client_logged_in.post(f"/movement/{doc.id}/receive/ozon-api").get_data(as_text=True)
    assert "данные WB API (поставка 30000007)" in html
    import re
    from html import unescape
    payload = unescape(re.search(r'name="act_payload" value="([^"]*)"', html).group(1))
    client_logged_in.post(f"/movement/{doc.id}/receive", data={"act_payload": payload})
    db.session.expire_all()
    doc = MovementDocument.query.get(doc.id)
    assert doc.received_at is not None and doc.receipt_returned_boxes == boxes[1].box_number


# ---- настройки и статусы ----------------------------------------------------

def _jwt(payload):
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJFUzI1NiJ9.{body}.signature"


def test_wb_settings_save_token_and_describe(db, client_logged_in):
    token = _jwt({"acc": 3, "s": (1 << 10) | (1 << 2), "exp": 1800918214})
    client_logged_in.post("/integrations/wb/", data={"token": token})
    assert AppSetting.query.get("wb_api_token").value == token
    html = client_logged_in.get("/integrations/wb/").get_data(as_text=True)
    assert "персональный" in html and "Поставки" in html and token not in html


def test_wb_settings_warns_without_supplies_scope(db, client_logged_in):
    resp = client_logged_in.post("/integrations/wb/", data={"token": _jwt({"acc": 1, "s": 2})}, follow_redirects=True)
    assert "нет категории «Поставки»" in resp.get_data(as_text=True)


def test_wb_statuses_on_review_page(db, client_logged_in):
    from wms.blueprints import movement as movement_bp

    movement_bp._wb_states_cache.update({"at": None, "states": {}})
    sender, dest = _wb_world(8)
    n1 = _nomenclature(110)
    doc, _ = _make_doc("PER-WB0008", "30000008", sender, dest, [[(n1, 1)]])
    with _Patches(supplies=[{"supplyID": 30000008, "statusID": 5}]):
        data = client_logged_in.get("/movement/receive-review/ozon-states").get_json()
    assert data["states"][str(doc.id)]["label"] == "WB: Принято"
    assert data["states"][str(doc.id)]["state"] == "WB_5"
