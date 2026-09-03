from flask import Blueprint, render_template

from ..models import Box, Cell, MovementDocument, Nomenclature, ReceivingDocument, Warehouse

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    stats = {
        "warehouses": Warehouse.query.count(),
        "cells": Cell.query.count(),
        "nomenclature": Nomenclature.query.count(),
        "boxes": Box.query.count(),
        "receiving_drafts": ReceivingDocument.query.filter_by(status="draft").count(),
        "movements": MovementDocument.query.count(),
    }
    recent_receiving = (
        ReceivingDocument.query.order_by(ReceivingDocument.created_at.desc()).limit(5).all()
    )
    recent_movement = (
        MovementDocument.query.order_by(MovementDocument.created_at.desc()).limit(5).all()
    )
    return render_template(
        "index.html",
        stats=stats,
        recent_receiving=recent_receiving,
        recent_movement=recent_movement,
    )
