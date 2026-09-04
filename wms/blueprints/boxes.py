from flask import Blueprint, render_template

from ..models import Box

bp = Blueprint("boxes", __name__)


@bp.route("/<int:box_id>")
def detail(box_id):
    box = Box.query.get_or_404(box_id)
    items = box.items.all()
    return render_template("boxes/detail.html", box=box, items=items)
