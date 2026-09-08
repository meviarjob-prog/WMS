from flask import Blueprint, jsonify, request

from ..extensions import db
from ..models import Box, Cell, Nomenclature

bp = Blueprint("api", __name__)


@bp.route("/nomenclature/search")
def search_nomenclature():
    """Поиск номенклатуры вручную: по каждому слову запроса отдельно —
    оно должно встретиться (как подстрока) в артикуле, наименовании или
    штрихкоде, слова могут идти в любом порядке. Так "кар беж" находит
    "Кардиган бежевый 44-45", хотя такой подстроки целиком в названии нет."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    tokens = q.split()
    conditions = [
        db.or_(
            Nomenclature.name.ilike(f"%{token}%"),
            Nomenclature.sku.ilike(f"%{token}%"),
            Nomenclature.barcode.ilike(f"%{token}%"),
        )
        for token in tokens
    ]
    items = (
        Nomenclature.query.filter(db.and_(*conditions))
        .order_by(Nomenclature.name)
        .limit(20)
        .all()
    )
    return jsonify(
        [
            {
                "id": i.id,
                "sku": i.sku,
                "barcode": i.barcode,
                "name": i.name,
                "unit": i.unit,
                "label": f"{i.name} (арт. {i.sku}, шк. {i.barcode})",
            }
            for i in items
        ]
    )


@bp.route("/nomenclature/by-barcode/<barcode>")
def nomenclature_by_barcode(barcode):
    """Точный поиск товара по штрихкоду (для сканера)."""
    item = Nomenclature.query.filter_by(barcode=barcode.strip()).first()
    if not item:
        return jsonify({"found": False}), 404
    return jsonify(
        {
            "found": True,
            "id": item.id,
            "sku": item.sku,
            "barcode": item.barcode,
            "name": item.name,
            "unit": item.unit,
        }
    )


@bp.route("/box/by-number/<box_number>")
def box_by_number(box_number):
    box = Box.find_by_scanned_code(box_number)
    if not box:
        return jsonify({"found": False}), 404
    return jsonify(
        {
            "found": True,
            "id": box.id,
            "box_number": box.box_number,
            "status": box.status,
            "warehouse": box.warehouse.name if box.warehouse else None,
            "warehouse_id": box.warehouse_id,
            "cell": box.cell.code if box.cell else None,
            "cell_id": box.cell_id,
            "total_qty": box.total_qty(),
        }
    )


@bp.route("/cells/search")
def search_cells():
    """Поиск ячеек по вхождению кода, опционально ограничено складом."""
    q = request.args.get("q", "").strip()
    warehouse_id = request.args.get("warehouse_id", type=int)

    query = Cell.query.filter_by(is_active=True)
    if warehouse_id:
        query = query.filter_by(warehouse_id=warehouse_id)
    if q:
        query = query.filter(Cell.code.ilike(f"%{q}%"))

    cells = query.order_by(Cell.code).limit(20).all()
    return jsonify(
        [{"id": c.id, "code": c.code, "warehouse_id": c.warehouse_id} for c in cells]
    )


@bp.route("/cells/by-code")
def cell_by_code():
    code = request.args.get("code", "").strip()
    warehouse_id = request.args.get("warehouse_id", type=int)
    if not code:
        return jsonify({"found": False}), 404

    query = Cell.query.filter_by(code=code)
    if warehouse_id:
        query = query.filter_by(warehouse_id=warehouse_id)
    cell = query.first()
    if not cell:
        return jsonify({"found": False}), 404
    return jsonify({"found": True, "id": cell.id, "code": cell.code, "warehouse_id": cell.warehouse_id})
