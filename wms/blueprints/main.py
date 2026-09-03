from flask import Blueprint, render_template

from ..extensions import db
from ..models import (
    Box,
    Cell,
    MovementDocument,
    Nomenclature,
    PlacementDocument,
    ReceivingDocument,
    UnplacedStock,
    Warehouse,
)

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    stats = {
        "warehouses": Warehouse.query.count(),
        "cells": Cell.query.count(),
        "nomenclature": Nomenclature.query.count(),
        "boxes": Box.query.count(),
        "unplaced_items": db.session.query(db.func.count(UnplacedStock.id))
        .filter(UnplacedStock.qty > 0)
        .scalar()
        or 0,
        "open_boxes": Box.query.filter_by(cell_id=None).count(),
    }
    recent_receiving = (
        ReceivingDocument.query.order_by(ReceivingDocument.created_at.desc()).limit(5).all()
    )
    recent_placement = (
        PlacementDocument.query.order_by(PlacementDocument.created_at.desc()).limit(5).all()
    )
    recent_movement = (
        MovementDocument.query.order_by(MovementDocument.created_at.desc()).limit(5).all()
    )
    return render_template(
        "index.html",
        stats=stats,
        recent_receiving=recent_receiving,
        recent_placement=recent_placement,
        recent_movement=recent_movement,
    )
