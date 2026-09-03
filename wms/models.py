from datetime import datetime

from .extensions import db


class Counter(db.Model):
    """Хранит последнее значение для генерации номеров документов/коробов."""

    __tablename__ = "counters"

    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Integer, nullable=False, default=0)


class Warehouse(db.Model):
    __tablename__ = "warehouses"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    address = db.Column(db.String(300))
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    cells = db.relationship("Cell", backref="warehouse", lazy="dynamic")

    def __repr__(self):
        return f"<Warehouse {self.code}>"


class Cell(db.Model):
    __tablename__ = "cells"

    id = db.Column(db.Integer, primary_key=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    code = db.Column(db.String(50), nullable=False)
    description = db.Column(db.String(200))
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    boxes = db.relationship("Box", backref="cell", lazy="dynamic")

    __table_args__ = (
        db.UniqueConstraint("warehouse_id", "code", name="uq_cell_warehouse_code"),
    )

    def __repr__(self):
        return f"<Cell {self.code}>"


class Nomenclature(db.Model):
    __tablename__ = "nomenclature"

    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(50), unique=True, nullable=False)
    barcode = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(300), nullable=False)
    unit = db.Column(db.String(20), nullable=False, default="шт")
    description = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<Nomenclature {self.sku} {self.name}>"


class Box(db.Model):
    __tablename__ = "boxes"

    id = db.Column(db.Integer, primary_key=True)
    box_number = db.Column(db.String(30), unique=True, nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    cell_id = db.Column(db.Integer, db.ForeignKey("cells.id"), nullable=True)
    receiving_document_id = db.Column(
        db.Integer, db.ForeignKey("receiving_documents.id"), nullable=True
    )
    status = db.Column(db.String(20), nullable=False, default="open")  # open | stored
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    warehouse = db.relationship("Warehouse", foreign_keys=[warehouse_id])
    items = db.relationship(
        "BoxItem", backref="box", lazy="dynamic", cascade="all, delete-orphan"
    )

    def total_qty(self):
        return sum(item.qty for item in self.items)

    def __repr__(self):
        return f"<Box {self.box_number}>"


class BoxItem(db.Model):
    __tablename__ = "box_items"

    id = db.Column(db.Integer, primary_key=True)
    box_id = db.Column(db.Integer, db.ForeignKey("boxes.id"), nullable=False)
    nomenclature_id = db.Column(db.Integer, db.ForeignKey("nomenclature.id"), nullable=False)
    qty = db.Column(db.Float, nullable=False, default=0)

    nomenclature = db.relationship("Nomenclature")


class ReceivingDocument(db.Model):
    __tablename__ = "receiving_documents"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(30), unique=True, nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    supplier = db.Column(db.String(200))
    status = db.Column(db.String(20), nullable=False, default="draft")  # draft | completed
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

    warehouse = db.relationship("Warehouse")
    lines = db.relationship(
        "ReceivingLine", backref="document", lazy="dynamic", cascade="all, delete-orphan"
    )
    boxes = db.relationship(
        "Box", backref="receiving_document", lazy="dynamic",
        foreign_keys="Box.receiving_document_id",
    )

    def total_qty(self):
        return sum(line.qty for line in self.lines)


class ReceivingLine(db.Model):
    __tablename__ = "receiving_lines"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(
        db.Integer, db.ForeignKey("receiving_documents.id"), nullable=False
    )
    nomenclature_id = db.Column(db.Integer, db.ForeignKey("nomenclature.id"), nullable=False)
    qty = db.Column(db.Float, nullable=False, default=0)
    box_id = db.Column(db.Integer, db.ForeignKey("boxes.id"), nullable=True)

    nomenclature = db.relationship("Nomenclature")
    box = db.relationship("Box")


class MovementDocument(db.Model):
    __tablename__ = "movement_documents"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(30), unique=True, nullable=False)
    box_id = db.Column(db.Integer, db.ForeignKey("boxes.id"), nullable=False)

    from_warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"))
    from_cell_id = db.Column(db.Integer, db.ForeignKey("cells.id"))
    to_warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    to_cell_id = db.Column(db.Integer, db.ForeignKey("cells.id"))

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    box = db.relationship("Box")
    from_warehouse = db.relationship("Warehouse", foreign_keys=[from_warehouse_id])
    to_warehouse = db.relationship("Warehouse", foreign_keys=[to_warehouse_id])
    from_cell = db.relationship("Cell", foreign_keys=[from_cell_id])
    to_cell = db.relationship("Cell", foreign_keys=[to_cell_id])
