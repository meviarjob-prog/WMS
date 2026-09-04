from datetime import date, datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


class Counter(db.Model):
    """Хранит последнее значение для генерации номеров документов/коробов."""

    __tablename__ = "counters"

    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Integer, nullable=False, default=0)


class AppSetting(db.Model):
    """Простые настройки приложения в виде ключ-значение (например,
    задержка между сканами на производстве) — редактируются администратором,
    без отдельной формы под каждую настройку."""

    __tablename__ = "app_settings"

    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(200))


class ProductCategory(db.Model):
    """Вид товара (свитер/кардиган/шапка/...) — определяется автоматически
    по вхождению ключевого слова в название товара при создании/импорте
    номенклатуры. У каждого вида своя норма времени на 1 шт для расчета
    эффективности на производстве (используется, если у конкретного товара
    норма не задана явно)."""

    __tablename__ = "product_categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    # Ключевые слова через запятую для автоопределения по названию товара
    # (регистронезависимое вхождение подстроки), например "свитер,свитера".
    keywords = db.Column(db.String(300))
    norm_minutes = db.Column(db.Float, nullable=True)
    # Категория-заглушка: присваивается товару, если ни одно ключевое слово
    # других категорий не подошло. Должна быть ровно одна такая категория.
    is_default = db.Column(db.Boolean, nullable=False, default=False)

    def __repr__(self):
        return f"<ProductCategory {self.name}>"


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(200))
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    is_active_user = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Плановая длительность смены (минут) — используется для расчета
    # эффективности в модуле «Производство» (норма-минуты / эта величина).
    shift_minutes = db.Column(db.Integer, nullable=False, default=480)
    # "warehouse" — обычный доступ ко всем разделам (как раньше);
    # "production" — ограниченный доступ: только сканирование ЧЗ на
    # производстве, ничего больше (проверяется в before_request). Админ
    # (is_admin=True) всегда имеет полный доступ независимо от role.
    role = db.Column(db.String(20), nullable=False, default="warehouse")

    def is_production_only(self):
        return self.role == "production" and not self.is_admin

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    # UserMixin.is_active — Flask-Login проверяет это свойство при загрузке
    # пользователя из сессии; свою колонку называем иначе, чтобы не путать
    # с зарезервированным именем.
    @property
    def is_active(self):
        return self.is_active_user

    def display_name(self):
        return self.full_name or self.username

    def __repr__(self):
        return f"<User {self.username}>"


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


class Zone(db.Model):
    """Зона склада — объединяет несколько ячеек (стеллаж/ряд/участок).
    Печатается как крупная A4-этикетка для навешивания на стеллаж/вход в зону."""

    __tablename__ = "zones"

    id = db.Column(db.Integer, primary_key=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    code = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(200))
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    warehouse = db.relationship("Warehouse")
    cells = db.relationship("Cell", backref="zone", lazy="dynamic")

    __table_args__ = (
        db.UniqueConstraint("warehouse_id", "code", name="uq_zone_warehouse_code"),
    )

    def __repr__(self):
        return f"<Zone {self.code}>"


class Cell(db.Model):
    __tablename__ = "cells"

    id = db.Column(db.Integer, primary_key=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    zone_id = db.Column(db.Integer, db.ForeignKey("zones.id"), nullable=True)
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
    # Норма времени на изготовление 1 шт (минут) — используется в модуле
    # «Производство» для расчета эффективности сотрудника. Если не задана —
    # берется норма вида товара (category.norm_minutes).
    norm_minutes = db.Column(db.Float, nullable=True)
    # Вид товара (свитер/кардиган/шапка/...) — определяется автоматически
    # по названию при создании/импорте, задает норму по умолчанию.
    category_id = db.Column(db.Integer, db.ForeignKey("product_categories.id"), nullable=True)

    category = db.relationship("ProductCategory")

    def effective_norm_minutes(self):
        if self.norm_minutes is not None:
            return self.norm_minutes
        return self.category.norm_minutes if self.category else None

    def __repr__(self):
        return f"<Nomenclature {self.sku} {self.name}>"


class UnplacedStock(db.Model):
    """Остаток принятого, но еще не размещенного в коробах/ячейках товара
    по складу в целом (без привязки к конкретному документу приемки)."""

    __tablename__ = "unplaced_stock"

    id = db.Column(db.Integer, primary_key=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    nomenclature_id = db.Column(db.Integer, db.ForeignKey("nomenclature.id"), nullable=False)
    qty = db.Column(db.Float, nullable=False, default=0)

    warehouse = db.relationship("Warehouse")
    nomenclature = db.relationship("Nomenclature")

    __table_args__ = (
        db.UniqueConstraint("warehouse_id", "nomenclature_id", name="uq_unplaced_wh_item"),
    )

    @staticmethod
    def add(warehouse_id, nomenclature_id, qty):
        row = UnplacedStock.query.filter_by(
            warehouse_id=warehouse_id, nomenclature_id=nomenclature_id
        ).first()
        if row is None:
            row = UnplacedStock(
                warehouse_id=warehouse_id, nomenclature_id=nomenclature_id, qty=0
            )
            db.session.add(row)
        row.qty += qty
        return row

    @staticmethod
    def available(warehouse_id, nomenclature_id):
        row = UnplacedStock.query.filter_by(
            warehouse_id=warehouse_id, nomenclature_id=nomenclature_id
        ).first()
        return row.qty if row else 0


class Box(db.Model):
    __tablename__ = "boxes"

    id = db.Column(db.Integer, primary_key=True)
    box_number = db.Column(db.String(30), unique=True, nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    cell_id = db.Column(db.Integer, db.ForeignKey("cells.id"), nullable=True)
    placement_document_id = db.Column(
        db.Integer, db.ForeignKey("placement_documents.id"), nullable=True
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
    """Приемка товара — только количество по позициям, без коробов и ячеек.
    Размещение принятого товара в короба/ячейки выполняется отдельной
    операцией «Размещение» (см. PlacementDocument)."""

    __tablename__ = "receiving_documents"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(30), unique=True, nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    supplier = db.Column(db.String(200))
    status = db.Column(db.String(20), nullable=False, default="draft")  # draft | completed
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

    warehouse = db.relationship("Warehouse")
    created_by = db.relationship("User")
    lines = db.relationship(
        "ReceivingLine", backref="document", lazy="dynamic", cascade="all, delete-orphan"
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

    nomenclature = db.relationship("Nomenclature")


class PlacementDocument(db.Model):
    """Размещение товара: берет общий неразмещенный остаток по складу,
    упаковывает в короба и расставляет короба по ячейкам. Не привязано к
    конкретному документу приемки."""

    __tablename__ = "placement_documents"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(30), unique=True, nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="draft")  # draft | completed
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

    warehouse = db.relationship("Warehouse")
    created_by = db.relationship("User")
    lines = db.relationship(
        "PlacementLine", backref="document", lazy="dynamic", cascade="all, delete-orphan"
    )
    boxes = db.relationship(
        "Box", backref="placement_document", lazy="dynamic",
        foreign_keys="Box.placement_document_id",
    )


class PlacementLine(db.Model):
    """Позиция размещения: часть неразмещенного остатка, взятая под упаковку
    в короб. box_id проставляется в момент упаковки в конкретный короб."""

    __tablename__ = "placement_lines"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(
        db.Integer, db.ForeignKey("placement_documents.id"), nullable=False
    )
    nomenclature_id = db.Column(db.Integer, db.ForeignKey("nomenclature.id"), nullable=False)
    qty = db.Column(db.Float, nullable=False, default=0)
    box_id = db.Column(db.Integer, db.ForeignKey("boxes.id"), nullable=True)

    nomenclature = db.relationship("Nomenclature")
    box = db.relationship("Box")


class MovementDocument(db.Model):
    """Перемещение — список коробов, едущих со склада-отправителя на склад
    назначения (оба выбираются один раз для всего документа). Короба
    сканируются и добавляются в список по одному; весь товар внутри короба
    переезжает вместе с ним."""

    __tablename__ = "movement_documents"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(30), unique=True, nullable=False)
    from_warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    to_warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="draft")  # draft | completed
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

    from_warehouse = db.relationship("Warehouse", foreign_keys=[from_warehouse_id])
    to_warehouse = db.relationship("Warehouse", foreign_keys=[to_warehouse_id])
    created_by = db.relationship("User")
    lines = db.relationship(
        "MovementLine", backref="document", lazy="dynamic", cascade="all, delete-orphan"
    )


class MovementLine(db.Model):
    """Один отсканированный короб в списке перемещения. from_* — снимок
    расположения короба на момент сканирования, to_cell_id — ячейка
    назначения на складе документа (можно указать сразу или позже, до
    завершения документа)."""

    __tablename__ = "movement_lines"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("movement_documents.id"), nullable=False)
    box_id = db.Column(db.Integer, db.ForeignKey("boxes.id"), nullable=False)

    from_warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"))
    from_cell_id = db.Column(db.Integer, db.ForeignKey("cells.id"))
    to_cell_id = db.Column(db.Integer, db.ForeignKey("cells.id"))

    box = db.relationship("Box")
    from_warehouse = db.relationship("Warehouse", foreign_keys=[from_warehouse_id])
    from_cell = db.relationship("Cell", foreign_keys=[from_cell_id])
    to_cell = db.relationship("Cell", foreign_keys=[to_cell_id])


class ProductionRecord(db.Model):
    """Одна собранная сотрудником единица товара на производстве.

    Сканируется обычный штрихкод товара — он одинаков у всех единиц
    одного артикула, поэтому надежно исключить накрутку по количеству
    (как это делает уникальный код) нельзя. Вместо этого — простая защита
    от случайных повторных сканов: минимальный интервал между двумя
    сканами одного сотрудника (см. production._scan_cooldown_seconds()),
    настраиваемый администратором."""

    __tablename__ = "production_records"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    nomenclature_id = db.Column(db.Integer, db.ForeignKey("nomenclature.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Рабочий день, к которому относится запись (для группировки по сменам
    # в отчете эффективности) — отдельно от created_at на случай смены
    # после полуночи.
    work_date = db.Column(db.Date, nullable=False, default=date.today)

    user = db.relationship("User")
    nomenclature = db.relationship("Nomenclature")

    def __repr__(self):
        return f"<ProductionRecord {self.id}>"


class InventoryDocument(db.Model):
    """Лист инвентаризации по складу: сканируются короба один за другим,
    товар внутри каждого короба автоматически суммируется в общий список
    (одинаковые товары из разных коробов складываются в одну строку)."""

    __tablename__ = "inventory_documents"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(30), unique=True, nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="draft")  # draft | completed
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

    warehouse = db.relationship("Warehouse")
    created_by = db.relationship("User")
    lines = db.relationship(
        "InventoryLine", backref="document", lazy="dynamic", cascade="all, delete-orphan"
    )
    scanned_boxes = db.relationship(
        "InventoryScannedBox", backref="document", lazy="dynamic", cascade="all, delete-orphan"
    )

    def total_qty(self):
        return sum(line.qty for line in self.lines)


class InventoryLine(db.Model):
    """Одна строка агрегированного списка — суммарное количество товара по
    всем отсканированным в этом документе коробам."""

    __tablename__ = "inventory_lines"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("inventory_documents.id"), nullable=False)
    nomenclature_id = db.Column(db.Integer, db.ForeignKey("nomenclature.id"), nullable=False)
    qty = db.Column(db.Float, nullable=False, default=0)

    nomenclature = db.relationship("Nomenclature")

    __table_args__ = (
        db.UniqueConstraint("document_id", "nomenclature_id", name="uq_inventory_doc_item"),
    )


class InventoryScannedBox(db.Model):
    """Какие короба уже учтены в этом документе — не дает посчитать один и
    тот же короб дважды при повторном/случайном скане."""

    __tablename__ = "inventory_scanned_boxes"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("inventory_documents.id"), nullable=False)
    box_id = db.Column(db.Integer, db.ForeignKey("boxes.id"), nullable=False)
    scanned_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    box = db.relationship("Box")

    __table_args__ = (
        db.UniqueConstraint("document_id", "box_id", name="uq_inventory_doc_box"),
    )
