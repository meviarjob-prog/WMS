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
    # Порог количества этого вида товара в ОДНОМ коробе при приемке — если
    # суммарно в коробе превышено, приемщику показывается предупреждение
    # (см. receiving._box_category_warning): вероятно, лишний скан или
    # ошибка, а не настоящая такая партия. NULL — предупреждение не нужно.
    box_qty_warning = db.Column(db.Float, nullable=True)

    def __repr__(self):
        return f"<ProductCategory {self.name}>"


# Разделы, доступ к которым можно ограничивать по отдельности для
# конкретного пользователя (см. User.allowed_sections) — код совпадает с
# именем blueprint'а, чтобы before_request мог проверять его напрямую по
# request.endpoint, без отдельной таблицы соответствий.
SECTIONS = [
    ("nomenclature", "Номенклатура (и «Где товар»)"),
    ("warehouses", "Склады и ячейки"),
    ("receiving", "Приемка"),
    ("placement", "Размещение"),
    ("movement", "Перемещение"),
    ("inventory", "Инвентаризация"),
    ("production", "Производство"),
    ("reports", "Отчеты"),
    ("shipment_plan", "План отгрузок"),
]
SECTION_CODES = {code for code, _ in SECTIONS}


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
    # Точечное ограничение доступа к разделам для role="warehouse":
    # NULL/пусто — доступ ко всем разделам (как раньше, обратная
    # совместимость для уже существующих пользователей); "none" — ни одного
    # раздела из SECTIONS; иначе — список кодов через запятую. Роль
    # "production" и is_admin это поле игнорируют — у них доступ уже решен
    # отдельно (is_production_only / полный доступ администратора).
    allowed_sections = db.Column(db.Text, nullable=True)
    # Право редактировать номенклатуру (создавать/менять вид и норму,
    # импортировать из Excel) — отдельно от доступа к разделу "nomenclature"
    # как таковому: с этим флагом снятым пользователь по-прежнему видит
    # список и "Где товар", но не может ничего в номенклатуре менять.
    # По умолчанию True — как и было для всех до появления этого флага
    # (см. _ensure_columns: у уже существующих пользователей после
    # миграции тоже принудительно выставляется True, а не NULL).
    nomenclature_edit_allowed = db.Column(db.Boolean, nullable=False, default=True)
    # Отдельное право на редактирование соответствия складов WMS складам
    # 1С. Сама страница складов доступна по allowed_sections, а эта галочка
    # разрешает только чувствительную интеграционную настройку.
    warehouse_mapping_allowed = db.Column(db.Boolean, nullable=False, default=False)
    # Право видеть все приемки, созданные из накладных. Используется для
    # приемщиков и заведующих складом; ручные приемки других сотрудников
    # этот флаг не открывает.
    invoice_receiving_view_allowed = db.Column(db.Boolean, nullable=False, default=False)
    # Право просматривать перемещения всех сотрудников. Изменение чужих
    # документов этим правом не разрешается.
    movement_view_allowed = db.Column(db.Boolean, nullable=False, default=False)
    # Право завершать чужие перемещения и отмечать "Принято на складе" (см.
    # movement.complete/receive) — в отличие от movement_view_allowed (только
    # просмотр), это право позволяет менять документ. Дает и просмотр тоже
    # (см. movement._can_view_movement_document) — без него не добраться до
    # кнопок на детальной странице.
    movement_complete_allowed = db.Column(db.Boolean, nullable=False, default=False)
    # Отдельное право подтверждать фактическую приемку перемещения на
    # складе назначения. Не связано с правом завершать сборку.
    movement_receive_allowed = db.Column(db.Boolean, nullable=True, default=None)
    # Отдельный доступ к сводной панели руководителя. Операционные отчеты
    # могут быть доступны сотруднику, но финансово-управленческая сводка
    # при этом остается скрытой.
    management_dashboard_allowed = db.Column(db.Boolean, nullable=False, default=False)
    # Рабочий склад сотрудника. Для перемещений он всегда становится
    # складом-отправителем, поэтому сотрудник не может случайно собрать
    # документ от имени другого склада.
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=True)
    # Версия входа используется для принудительного завершения сессий.
    # Она записывается в cookie при авторизации; увеличение значения делает
    # все ранее выданные cookie пользователя недействительными.
    session_version = db.Column(db.Integer, nullable=False, default=0)

    warehouse = db.relationship("Warehouse", foreign_keys=[warehouse_id])

    def is_production_only(self):
        return self.role == "production" and not self.is_admin

    def is_logist_only(self):
        """Логист видит только перемещения, ожидающие передачи транспорту
        (movement.transport_list) — ничего больше в WMS, см. чат. Проверяется
        в before_request так же, как is_production_only()."""
        return self.role == "logist" and not self.is_admin

    def allowed_section_set(self):
        if not self.allowed_sections:
            return set(SECTION_CODES)
        if self.allowed_sections == "none":
            return set()
        return set(self.allowed_sections.split(","))

    def can_edit_nomenclature(self):
        # nomenclature_edit_allowed is not False (а не просто truthy) — на
        # случай, если колонка у какой-то строки все же осталась NULL
        # (ALTER TABLE ADD COLUMN не проставляет DEFAULT задним числом),
        # трактуем это как "не запрещено", а не как "запрещено".
        return self.is_admin or self.nomenclature_edit_allowed is not False

    def can_manage_warehouse_mapping(self):
        return self.is_admin or self.warehouse_mapping_allowed is True

    def can_view_invoice_receivings(self):
        return self.is_admin or self.invoice_receiving_view_allowed is True

    def can_view_movements(self):
        return self.is_admin or self.movement_view_allowed is True

    def can_complete_movements(self):
        return self.is_admin or self.movement_complete_allowed is True

    def can_receive_movements(self):
        # NULL — пользователь существовал до разделения старого общего
        # права; сохраняем прежнее поведение до первого сохранения формы.
        return self.is_admin or self.movement_receive_allowed is True or (
            self.movement_receive_allowed is None and self.movement_complete_allowed is True
        )

    def can_view_management_dashboard(self):
        return self.is_admin or self.management_dashboard_allowed is True

    def has_section_access(self, section):
        """Раздел не из SECTIONS (например, служебные api/boxes/labels) не
        ограничивается этим механизмом вообще — управляются им только
        разделы верхнего меню."""
        if self.is_admin or section not in SECTION_CODES:
            return True
        if not self.allowed_sections:
            return True
        return section in self.allowed_sections.split(",")

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
    # Заполнено только для складов-городов, созданных автоматически при
    # загрузке плана отгрузок (см. ShipmentPlan) — "ozon"/"wb". У обычных
    # физических складов оба поля пустые. marketplace_city хранит
    # каноническое направление без префикса площадки; регистр, пробелы и
    # алиасы нормализуются при импорте плана.
    marketplace = db.Column(db.String(20), nullable=True)
    marketplace_city = db.Column(db.String(100), nullable=True)
    # Свободный текст (название организации/адрес/телефон) — печатается на
    # стикерах отправления (см. movement.export_shipping_labels) как
    # получатель этого склада-направления. Настраивается отдельно для
    # каждого склада, в т.ч. складов-городов маркетплейсов.
    recipient_info = db.Column(db.String(300), nullable=True)
    # Название соответствующего склада в 1С (например "Товары в пути ФФ
    # КАЗАНЬ, Взлётная 30") — 1С не заводит отдельный физический склад под
    # каждый склад-город маркетплейса из WMS, а использует свой набор
    # промежуточных складов. Настраивается отдельно на КАЖДЫЙ склад-город
    # (не на город целиком) — одна и та же площадка одного города может
    # ехать на разные склады 1С в зависимости от маркетплейса (например, ВБ
    # Краснодар — на СЦ, а ОЗОН Краснодар — на фулфилмент), а несколько
    # разных городов WMS (например Черкесск и Пятигорск) могут при этом
    # указывать на один и тот же склад 1С. Настраивается администратором на
    # странице «Настройки» (см. warehouses.update_fulfillment_1c_name) — по
    # умолчанию проставляется автоматически для известных городов (см.
    # warehouses.FULFILLMENT_1C_DEFAULTS), но полностью редактируемо. Пусто —
    # используется общий запасной склад (см.
    # integration_1c.FULFILLMENT_WAREHOUSE_NAME).
    fulfillment_1c_name = db.Column(db.String(200), nullable=True)

    cells = db.relationship("Cell", backref="warehouse", lazy="dynamic")

    def marketplace_label(self):
        return {"ozon": "ОЗОН", "wb": "ВБ"}.get(self.marketplace, "")

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
    # Короба, размещенные СРАЗУ в ряду, без конкретной ячейки — для
    # помещений, где нет возможности завести ячейки (см. чат). Ряд с нулем
    # ячеек уже можно было создать и раньше (cell_count=0 в форме), но до
    # этого поля разместить в него короб было нечем — только в ячейку.
    # У самого ряда, в отличие от ячейки, нет ограничения по вместимости.
    boxes = db.relationship("Box", backref="zone", lazy="dynamic")

    __table_args__ = (
        db.UniqueConstraint("warehouse_id", "code", name="uq_zone_warehouse_code"),
    )

    def __repr__(self):
        return f"<Zone {self.code}>"


# Сколько коробов физически помещается в одну ячейку — используется и для
# запрета переполнения при размещении, и для подсчета свободных мест при
# подсказке ячейки.
CELL_CAPACITY = 24


class Cell(db.Model):
    __tablename__ = "cells"

    id = db.Column(db.Integer, primary_key=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    zone_id = db.Column(db.Integer, db.ForeignKey("zones.id"), nullable=True, index=True)
    code = db.Column(db.String(50), nullable=False)
    description = db.Column(db.String(200))
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    boxes = db.relationship("Box", backref="cell", lazy="dynamic")

    __table_args__ = (
        db.UniqueConstraint("warehouse_id", "code", name="uq_cell_warehouse_code"),
    )

    def free_space(self, exclude_box_id=None):
        count = self.boxes.count()
        if exclude_box_id is not None and self.boxes.filter_by(id=exclude_box_id).first():
            count -= 1
        return CELL_CAPACITY - count

    def __repr__(self):
        return f"<Cell {self.code}>"


class Nomenclature(db.Model):
    __tablename__ = "nomenclature"

    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(50), unique=True, nullable=False)
    barcode = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(300), nullable=False)
    size = db.Column(db.String(20), nullable=True)
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
    def add(warehouse_id, nomenclature_id, qty, receiving_document=None):
        """receiving_document — если остаток пришел из конкретной приемки по
        накладной, заводим под него партию (см. UnplacedStockLot), чтобы
        потом можно было увидеть поставщика и номер заявки по остатку.
        Без него (возврат из "Размещения" при удалении документа/строки) —
        партия без источника: к этому моменту исходная партия уже
        перемешалась при упаковке в короб, восстановить её точно нельзя."""
        row = UnplacedStock.query.filter_by(
            warehouse_id=warehouse_id, nomenclature_id=nomenclature_id
        ).first()
        if row is None:
            row = UnplacedStock(
                warehouse_id=warehouse_id, nomenclature_id=nomenclature_id, qty=0
            )
            db.session.add(row)
        row.qty += qty
        if qty > 0:
            UnplacedStockLot.add(warehouse_id, nomenclature_id, qty, receiving_document)
        return row

    @staticmethod
    def consume(warehouse_id, nomenclature_id, qty):
        """Единая точка списания остатка — держит агрегат (для быстрых
        проверок available()) и партии (для истории поставщик/заявка) в
        синхроне. Вызывающая сторона отвечает за то, что qty не превышает
        available() — здесь только защита от ухода в минус."""
        if qty <= 0:
            return
        row = UnplacedStock.query.filter_by(
            warehouse_id=warehouse_id, nomenclature_id=nomenclature_id
        ).first()
        if row:
            row.qty = max(row.qty - qty, 0)
        UnplacedStockLot.consume(warehouse_id, nomenclature_id, qty)

    @staticmethod
    def available(warehouse_id, nomenclature_id):
        row = UnplacedStock.query.filter_by(
            warehouse_id=warehouse_id, nomenclature_id=nomenclature_id
        ).first()
        return row.qty if row else 0

    def active_lots(self):
        """Партии, из которых складывается этот остаток — поставщик и номер
        заявки по каждой (см. UnplacedStockLot); может быть пусто, если
        остаток когда-то создался без партий (до этой версии)."""
        return (
            UnplacedStockLot.query.filter_by(
                warehouse_id=self.warehouse_id, nomenclature_id=self.nomenclature_id
            )
            .filter(UnplacedStockLot.qty_remaining > 0)
            .order_by(UnplacedStockLot.received_at)
            .all()
        )


class UnplacedStockLot(db.Model):
    """Одна партия неразмещенного остатка — привязана к конкретной приемке
    (если она пришла из накладной), чтобы по остатку было видно поставщика
    и номер заявки. UnplacedStock остается быстрым агрегатом "сколько всего
    доступно"; партии обновляются синхронно через UnplacedStock.add()/
    consume() и списываются по очереди поступления (FIFO)."""

    __tablename__ = "unplaced_stock_lots"

    id = db.Column(db.Integer, primary_key=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False, index=True)
    nomenclature_id = db.Column(db.Integer, db.ForeignKey("nomenclature.id"), nullable=False, index=True)
    receiving_document_id = db.Column(db.Integer, db.ForeignKey("receiving_documents.id"), nullable=True)
    # Снимок на момент поступления — не ссылка на текущие supplier/
    # order_number документа, чтобы история не менялась задним числом,
    # если реквизиты приемки потом поправят.
    supplier_name = db.Column(db.String(200), nullable=True)
    order_number = db.Column(db.String(50), nullable=True)
    qty_received = db.Column(db.Float, nullable=False)
    qty_remaining = db.Column(db.Float, nullable=False)
    received_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    warehouse = db.relationship("Warehouse")
    nomenclature = db.relationship("Nomenclature")
    receiving_document = db.relationship("ReceivingDocument")

    @staticmethod
    def add(warehouse_id, nomenclature_id, qty, receiving_document=None):
        lot = UnplacedStockLot(
            warehouse_id=warehouse_id,
            nomenclature_id=nomenclature_id,
            receiving_document_id=receiving_document.id if receiving_document else None,
            supplier_name=receiving_document.supplier if receiving_document else None,
            order_number=receiving_document.order_number if receiving_document else None,
            qty_received=qty,
            qty_remaining=qty,
        )
        db.session.add(lot)
        return lot

    @staticmethod
    def consume(warehouse_id, nomenclature_id, qty):
        """Списывает qty по партиям этого товара на складе в порядке
        поступления (FIFO). Партий может не хватить (например, остаток
        когда-то создался без партий, до этой версии) — тогда списываем
        сколько есть и молча останавливаемся, не уводя партии в минус."""
        remaining = qty
        lots = (
            UnplacedStockLot.query.filter_by(
                warehouse_id=warehouse_id, nomenclature_id=nomenclature_id
            )
            .filter(UnplacedStockLot.qty_remaining > 0)
            .order_by(UnplacedStockLot.received_at)
            .all()
        )
        for lot in lots:
            if remaining <= 0:
                break
            take = min(lot.qty_remaining, remaining)
            lot.qty_remaining -= take
            remaining -= take
        return qty - remaining


class Box(db.Model):
    __tablename__ = "boxes"

    id = db.Column(db.Integer, primary_key=True)
    box_number = db.Column(db.String(30), unique=True, nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False, index=True)
    cell_id = db.Column(db.Integer, db.ForeignKey("cells.id"), nullable=True, index=True)
    # Ряд, в который короб размещен НАПРЯМУЮ, без конкретной ячейки — для
    # помещений без возможности завести ячейки (см. чат). Взаимоисключимо с
    # cell_id: расставленный короб имеет ЛИБО cell_id, ЛИБО zone_id, никогда
    # оба сразу (см. placement._place_box). Оба пустые — короб еще не
    # расставлен вовсе (как и раньше).
    zone_id = db.Column(db.Integer, db.ForeignKey("zones.id"), nullable=True, index=True)
    placement_document_id = db.Column(
        db.Integer, db.ForeignKey("placement_documents.id"), nullable=True, index=True
    )
    status = db.Column(db.String(20), nullable=False, default="open")  # open | stored
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Когда и кем короб был отсканирован в последний раз — в любой операции
    # (приемка, размещение, перемещение, инвентаризация), см. Box.mark_scanned.
    # Не история всех сканов, только последний — для полной истории у
    # инвентаризации есть отдельная InventoryScannedBox.scanned_at.
    last_scanned_at = db.Column(db.DateTime, nullable=True)
    last_scanned_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    warehouse = db.relationship("Warehouse", foreign_keys=[warehouse_id])
    last_scanned_by = db.relationship("User", foreign_keys=[last_scanned_by_id])
    items = db.relationship(
        "BoxItem", backref="box", lazy="dynamic", cascade="all, delete-orphan"
    )

    def total_qty(self):
        return sum(item.qty for item in self.items)

    def is_placed(self):
        return self.cell_id is not None or self.zone_id is not None

    def location_label(self):
        """Куда короб расставлен, для использования в середине фразы
        ("короб размещен в " + location_label()) — "ячейке <код>" либо, для
        короба напрямую в ряду без ячейки (см. zone_id), "ряду <код>".
        None — еще не расставлен."""
        if self.cell_id:
            return f"ячейке {self.cell.code}"
        if self.zone_id:
            return f"ряду {self.zone.code}"
        return None

    def mark_scanned(self, user):
        self.last_scanned_at = datetime.utcnow()
        self.last_scanned_by_id = user.id if user and user.is_authenticated else None

    @property
    def barcode_value(self):
        """Значение, которое реально кодируется в штрихкод короба — только
        цифры, без префикса "BOX-". Код128 при смешении букв и цифр
        переключается между наборами B/C прямо посередине кода, и часть
        сканеров считывает такой штрихкод с ошибкой (путает цифры).
        Чисто цифровой код кодируется одним набором C без переключений и
        читается надежно. box_number при этом остается как есть — печатается
        текстом под штрихкодом и используется как понятный человеку номер."""
        digits = "".join(ch for ch in self.box_number if ch.isdigit())
        return digits or self.box_number

    @staticmethod
    def find_by_scanned_code(code, warehouse_id=None):
        """Находит короб по отсканированному/введенному значению — принимает
        как полный номер ("BOX-000123", вручную с клавиатуры), так и чисто
        цифровой штрихкод ("000123", как реально закодировано в barcode_value
        начиная с этой правки) — иначе после смены формата штрихкода старые
        места ввода перестали бы находить короб по сканированию."""
        code = (code or "").strip()
        query = Box.query
        if warehouse_id is not None:
            query = query.filter_by(warehouse_id=warehouse_id)
        box = query.filter_by(box_number=code).first()
        if box is not None:
            return box
        if code.isdigit():
            # Реальный скан штрихкода — это именно цифровой код (см.
            # barcode_value), а не полный box_number, так что первый поиск
            # выше почти никогда не находит совпадение и раньше ВСЕГДА
            # проваливался в LIKE "%code" — а такой LIKE с ведущим "%" не
            # может использовать индекс и означает полное сканирование
            # таблицы boxes на КАЖДЫЙ скан короба. При десятках-сотнях тысяч
            # коробов это и давало заметные тормоза именно там, где сканируют
            # чаще всего (приемка, размещение, перемещение). Номер короба
            # всегда имеет вид "<префикс><цифры фиксированной ширины>"
            # (см. utils.numbering.SERIES["box"]), поэтому сначала пробуем
            # восстановить точный номер и найти его обычным (быстрым,
            # индексированным) точным совпадением.
            from .utils.numbering import SERIES

            prefix, width = SERIES["box"]
            if len(code) == width:
                box = query.filter_by(box_number=f"{prefix}{code}").first()
                if box is not None:
                    return box
            # Редкий случай (код нестандартной ширины/легаси-номер) —
            # полное сканирование как раньше, только если быстрый путь не сработал.
            box = query.filter(Box.box_number.like(f"%{code}")).first()
        return box

    def __repr__(self):
        return f"<Box {self.box_number}>"


class BoxItem(db.Model):
    __tablename__ = "box_items"

    id = db.Column(db.Integer, primary_key=True)
    box_id = db.Column(db.Integer, db.ForeignKey("boxes.id"), nullable=False, index=True)
    nomenclature_id = db.Column(db.Integer, db.ForeignKey("nomenclature.id"), nullable=False, index=True)
    qty = db.Column(db.Float, nullable=False, default=0)

    nomenclature = db.relationship("Nomenclature")


class Supplier(db.Model):
    """Справочник поставщиков — заполняется автоматически при загрузке
    приходной накладной (см. receiving.import_invoice), чтобы не вводить
    реквизиты вручную каждый раз для одного и того же поставщика. Поиск
    существующего при загрузке — сначала по ИНН (надежный уникальный
    идентификатор), и только если его нет в файле — по точному названию."""

    __tablename__ = "suppliers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(300), nullable=False)
    inn = db.Column(db.String(20), unique=True, nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<Supplier {self.name}>"


class ReceivingDocument(db.Model):
    """Приемка товара — только количество по позициям, без коробов и ячеек.
    Размещение принятого товара в короба/ячейки выполняется отдельной
    операцией «Размещение» (см. PlacementDocument)."""

    __tablename__ = "receiving_documents"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(30), unique=True, nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    supplier = db.Column(db.String(200))
    # Заполняется при загрузке приходной накладной (см. supplier — свободный
    # текст остается для отображения и для случаев ручного создания приемки,
    # когда справочника поставщиков еще может не быть).
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=True)
    # draft -> recounting -> sorting -> completed. "Разбраковка" (выделение
    # брака для возврата поставщику) возможна только на этапе sorting —
    # см. receiving.send_to_sorting/complete.
    status = db.Column(db.String(20), nullable=False, default="draft")
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    # Если приемка была открыта из инвентаризации пустого короба, после
    # завершения возвращаем сотрудника в тот же лист.
    return_inventory_id = db.Column(
        db.Integer, db.ForeignKey("inventory_documents.id"), nullable=True
    )
    return_inventory_box_id = db.Column(
        db.Integer, db.ForeignKey("boxes.id"), nullable=True
    )
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Когда документ перешел на пересчет/разбраковку — для истории статусов
    # на странице приемки (см. receiving.send_to_recount/send_to_sorting).
    # У приемок, завершенных до появления этих отметок, останутся пустыми.
    recounting_started_at = db.Column(db.DateTime, nullable=True)
    sorting_started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime)
    # Номер заказа поставщику ("№ заказа" в интерфейсе) — вносится вручную
    # при загрузке накладной,
    # т.к. в самом файле от 1С его нет (это внутренний номер, по которому
    # заказывали товар). Вместе с supplier это то, по чему потом ищут,
    # откуда взялся неразмещенный остаток (см. UnplacedStockLot).
    order_number = db.Column(db.String(50), nullable=True)
    # Сам файл накладной — чтобы можно было скачать оригинал позже (сверить
    # с бухгалтерией, разобрать спор с поставщиком и т.п.). Файлы 1С обычно
    # десятки-сотни КБ, так что даже при активной приемке это не заметная
    # нагрузка на БД (см. обсуждение в чате при внедрении).
    invoice_file_data = db.Column(db.LargeBinary, nullable=True)
    invoice_file_name = db.Column(db.String(255), nullable=True)
    # Заполняется, когда 1С подтвердила, что поправила количество в уже
    # заведенной приходной накладной под фактически принятое по итогам
    # пересчета WMS (см. integration_1c._receiving_adjustments_export и
    # SyncWMS.bsl СкорректироватьПриемку) — чтобы при повторной синхронизации
    # не отправлять одну и ту же корректировку снова.
    recount_synced_to_1c_at = db.Column(db.DateTime, nullable=True)
    # Ручное исключение корректировки из очереди выгрузки в 1С (см.
    # MovementDocument.accounting_entered_at) — например, бухгалтер уже
    # поправил количество в накладной сам и повторно выгружать не нужно.
    accounting_entered_at = db.Column(db.DateTime, nullable=True)
    # Отметка "проверено в 1С" — просто галочка для контроля бухгалтером
    # (см. чат), никак не влияет на сам документ и не связана с выгрузкой
    # (в отличие от accounting_entered_at выше и recount_synced_to_1c_at) —
    # только чтобы видеть в списке приемок, что документ уже сверили с 1С.
    checked_in_1c_at = db.Column(db.DateTime, nullable=True)

    warehouse = db.relationship("Warehouse")
    created_by = db.relationship("User")
    supplier_ref = db.relationship("Supplier")
    lines = db.relationship(
        "ReceivingLine", backref="document", lazy="dynamic", cascade="all, delete-orphan"
    )

    def total_qty(self):
        return sum(line.qty for line in self.lines)

    def is_from_invoice_import(self):
        """True — number это реальный номер накладной 1С (см.
        receiving.import_invoice_form), можно надежно синхронизировать
        возврат поставщику по номеру. False — number сгенерирован WMS
        (см. receiving.new_document), в 1С по нему ничего не найдется."""
        return self.invoice_file_name is not None


class ReceivingLine(db.Model):
    __tablename__ = "receiving_lines"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(
        db.Integer, db.ForeignKey("receiving_documents.id"), nullable=False, index=True
    )
    nomenclature_id = db.Column(db.Integer, db.ForeignKey("nomenclature.id"), nullable=False)
    qty = db.Column(db.Float, nullable=False, default=0)
    # Заполнено, если товар отсканирован сразу в короб при самой приемке
    # (см. receiving._receive_item_into_box) — тогда он уже лежит в коробе
    # и при завершении приемки НЕ уходит в неразмещенный остаток. Пусто —
    # обычная приемка "по количеству", разместить в короб позже вручную.
    box_id = db.Column(db.Integer, db.ForeignKey("boxes.id"), nullable=True)
    # Заполняется только при создании строки из накладной (см.
    # receiving.import_invoice) — сколько заявлено поставщиком, для
    # сравнения при приемке. NULL — строка добавлена вручную/сканированием,
    # сверять не с чем.
    expected_qty = db.Column(db.Float, nullable=True)
    # Отметка кладовщика "принято" на мобильной форме приемки по накладной
    # (см. receiving.confirm_invoice) — не влияет на остатки сама по себе,
    # только на прогресс сверки; остатки формирует qty при завершении
    # приемки, как обычно.
    confirmed = db.Column(db.Boolean, nullable=False, default=False)
    # Кол-во брака по этой строке, выявленное на этапе "Разбраковка" (см.
    # ReceivingDocument.status) — не может превышать qty. При завершении
    # приемки в неразмещенный остаток уходит только qty-defect_qty, а сам
    # брак фиксируется отдельным SupplierReturn. Для строк, упакованных в
    # короб при приемке (box_id заполнен), разбраковка не применяется —
    # остается 0.
    defect_qty = db.Column(db.Float, nullable=False, default=0)
    # Заполняется, когда строку завершили ПО ОТДЕЛЬНОСТИ на разбраковке (см.
    # receiving.complete_line) — не дожидаясь, пока проверят остальные
    # строки документа. Годное количество уже зачислено в неразмещенный
    # остаток и/или брак уже ушел в SupplierReturn — повторно эта строка при
    # обычном complete() не обрабатывается. NULL — строка еще не завершена
    # по отдельности (обычный путь: завершится вместе со всем документом).
    line_completed_at = db.Column(db.DateTime, nullable=True)
    # Один токен соответствует одному нажатию «Добавить». Повторная
    # отправка той же формы (двойной клик/зависший интернет) находит уже
    # созданную строку и не проводит приемку второй раз.
    request_token = db.Column(db.String(64), nullable=True)

    nomenclature = db.relationship("Nomenclature")
    box = db.relationship("Box")

    def good_qty(self):
        return max(self.qty - (self.defect_qty or 0), 0)


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
        db.Integer, db.ForeignKey("placement_documents.id"), nullable=False, index=True
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
    to_warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="draft")  # draft | collected | completed | merged
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    # Заполняется выгрузкой в 1С (см. api_1c) — чтобы при повторном нажатии
    # "Синхронизировать" в 1С не загрузить один и тот же документ дважды.
    synced_to_1c_at = db.Column(db.DateTime, nullable=True)
    # Заполняется отдельным действием "Принято на складе" — короб может
    # приехать (complete()), но физически его еще не проверили и не приняли
    # на складе назначения. Пока это поле пусто, выполнение плана отгрузок
    # по товару из этого документа не засчитывается (висит как "в пути"),
    # чтобы отгрузки не считались успешными до фактической приемки.
    received_at = db.Column(db.DateTime, nullable=True)
    # Отметка "внесено в 1С" — бухгалтерия может поставить ее вручную
    # (например, если ведет документ отдельно, в другой конфигурации), либо
    # она проставляется автоматически вместе с synced_to_1c_at, когда 1С
    # успешно забирает документ через API (см. export_confirm) — чтобы
    # бухгалтеру не нужно было дублировать галочку руками по факту, который
    # система и так подтвердила.
    accounting_entered_at = db.Column(db.DateTime, nullable=True)
    # Заполняется через export_confirm, когда 1С подтвердила создание
    # документа, но часть строк не сопоставилась с номенклатурой и была
    # пропущена (см. SyncWMS.bsl СоздатьПеремещениеТоваров — раньше одна
    # такая строка проваливала весь документ, теперь он создается по
    # совпавшим строкам, а несовпавшие видны здесь) — текст пришедших от
    # 1С предупреждений, показывается значком "!" в списке перемещений.
    # NULL — документ выгрузился полностью, без пропусков.
    sync_warning = db.Column(db.Text, nullable=True)
    # Заполняется, когда несколько параллельных черновиков на один и тот же
    # маршрут (тот же склад-отправитель и склад назначения — например,
    # несколько сотрудников собирали одно направление порознь) свели в один
    # итоговый документ — см. movement.merge_documents. Статус такого
    # документа становится "merged", его короба (MovementLine) переезжают
    # в итоговый документ, здесь остается только ссылка на него для истории.
    merged_into_id = db.Column(db.Integer, db.ForeignKey("movement_documents.id"), nullable=True)
    # Отметка "заявка на маркетплейс создана" — ручная галочка (см.
    # movement.toggle_marketplace_request), синего цвета в списке в отличие
    # от зеленой "1С" — независима от нее и от самой отправки, для
    # отдельного контроля за заявкой на приемку на стороне маркетплейса.
    marketplace_request_created_at = db.Column(db.DateTime, nullable=True)
    # Номер самой заявки на приемку у маркетплейса вносится вручную до
    # установки галочки выше. См. movement.update_marketplace_request_number.
    marketplace_request_number = db.Column(db.String(50), nullable=True)
    # Момент, когда транспорт физически забрал товар. Он отделен и от
    # подачи заявки на МП, и от последующей фактической приемки площадкой.
    shipped_at = db.Column(db.DateTime, nullable=True)
    # Заполняется, когда состав уже выгруженного в 1С документа меняют
    # (добавили/удалили короб — movement.add_box/delete_line, или поправили
    # количество в коробе, уже уехавшем этим перемещением — boxes.add_item/
    # update_item/move_item/delete_item) — см. integration_1c.
    # _movement_corrections_export. NULL — 1С видит актуальный состав, менять
    # ничего не нужно. Сбрасывается обратно в NULL, когда 1С подтверждает,
    # что скорректировала документ у себя (см. export_confirm).
    composition_changed_at = db.Column(db.DateTime, nullable=True)
    # Снимок количества на момент завершения сборки. Раньше total_sent_qty()
    # показывал именно его, но это разъезжалось с тем, что видно в экспортах
    # и с чем эти цифры сверяют по факту — заявками на самом маркетплейсе
    # (см. чат): если содержимое короба поправили уже после отправки, нужно
    # видеть актуальное состояние, а не то, что было отправлено изначально.
    # Поле оставлено (не читается для отображения), поскольку кто-то может
    # опираться на него отдельно — при необходимости используйте его явно.
    sent_qty_snapshot = db.Column(db.Float, nullable=True)
    received_qty_snapshot = db.Column(db.Float, nullable=True)

    from_warehouse = db.relationship("Warehouse", foreign_keys=[from_warehouse_id])
    to_warehouse = db.relationship("Warehouse", foreign_keys=[to_warehouse_id])
    created_by = db.relationship("User")
    merged_into = db.relationship("MovementDocument", remote_side=[id], backref="merged_from")
    lines = db.relationship(
        "MovementLine", backref="document", lazy="dynamic", cascade="all, delete-orphan"
    )

    def total_item_qty(self):
        """Суммарное количество товара (штук) во всех коробах документа — не
        путать с lines.count() (это количество коробов)."""
        box_ids = [line.box_id for line in self.lines]
        if not box_ids:
            return 0
        return (
            db.session.query(db.func.sum(BoxItem.qty))
            .filter(BoxItem.box_id.in_(box_ids))
            .scalar()
        ) or 0

    def total_received_qty(self):
        """Фактически принято на складе назначения с учетом расхождений."""
        if self.received_at is None:
            return None
        if self.received_qty_snapshot is not None:
            return self.received_qty_snapshot
        return self.total_item_qty() + sum(
            discrepancy.received_qty - discrepancy.expected_qty
            for discrepancy in self.discrepancies
        )

    def total_sent_qty(self):
        """Текущее количество товара в коробах документа — то же самое, что
        total_item_qty(). Раньше отдавал замороженный sent_qty_snapshot, но
        эти цифры сравнивают с заявками на самом маркетплейсе, а значит
        нужны актуальные данные, а не снимок на момент отправки (см. чат:
        "экспорт показывает 2220, строка показывает 2147" — после правки
        короба цифры разъехались)."""
        return self.total_item_qty()

    def total_shortage_qty(self):
        """Сколько товара не принято на складе назначения и нужно найти."""
        return sum(discrepancy.shortage_qty() for discrepancy in self.discrepancies)

    def total_plan_fact_qty(self):
        """Количество документа, которое может входить в факт плана.

        До передачи транспорту завершенный документ не считается отгрузкой.
        В пути учитываем состав коробов, после приемки — фактически принятое.
        """
        if self.status != "completed":
            return 0
        if self.received_at is not None:
            return self.total_received_qty()
        if self.shipped_at is not None:
            return self.total_item_qty()
        return 0


class MovementReceiptDiscrepancy(db.Model):
    """Расхождение между тем, что отправлено (по коробам документа), и тем,
    что реально приняли на складе назначения — по одной строке на товар.
    Заполняется кнопкой "Принято на складе" (см. movement.receive), когда
    фактически введенное количество не совпадает с тем, что было упаковано
    в коробах — недостача или излишек. Обычная приемка без расхождений
    (введенное количество совпало с ожидаемым) таких строк не создает
    вообще."""

    __tablename__ = "movement_receipt_discrepancies"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(
        db.Integer, db.ForeignKey("movement_documents.id"), nullable=False, index=True
    )
    nomenclature_id = db.Column(db.Integer, db.ForeignKey("nomenclature.id"), nullable=False)
    expected_qty = db.Column(db.Float, nullable=False)
    received_qty = db.Column(db.Float, nullable=False)

    document = db.relationship(
        "MovementDocument", backref=db.backref("discrepancies", cascade="all, delete-orphan")
    )
    nomenclature = db.relationship("Nomenclature")

    def diff(self):
        return self.received_qty - self.expected_qty

    def shortage_qty(self):
        return max(self.expected_qty - self.received_qty, 0)

    def excess_qty(self):
        return max(self.received_qty - self.expected_qty, 0)


class OneCQuantityCheck(db.Model):
    """Результат сверки количества документа в 1С с тем же документом WMS."""

    __tablename__ = "one_c_quantity_checks"

    id = db.Column(db.Integer, primary_key=True)
    document_type = db.Column(db.String(30), nullable=False, index=True)
    document_id = db.Column(db.Integer, nullable=False, index=True)
    document_number = db.Column(db.String(50), nullable=False)
    barcode = db.Column(db.String(100), nullable=True)
    item_name = db.Column(db.String(300), nullable=True)
    wms_qty = db.Column(db.Float, nullable=False, default=0)
    one_c_qty = db.Column(db.Float, nullable=False, default=0)
    checked_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def diff(self):
        return self.one_c_qty - self.wms_qty


class MovementLine(db.Model):
    """Один отсканированный короб в списке перемещения. from_* — снимок
    расположения короба на момент сканирования, to_cell_id — ячейка
    назначения на складе документа (можно указать сразу или позже, до
    завершения документа)."""

    __tablename__ = "movement_lines"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("movement_documents.id"), nullable=False, index=True)
    box_id = db.Column(db.Integer, db.ForeignKey("boxes.id"), nullable=False)
    # Когда короб был отсканирован именно в ЭТО перемещение — в отличие от
    # Box.last_scanned_at (перезаписывается любой последующей операцией),
    # эта отметка навсегда привязана к этой строке документа.
    scanned_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

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
    status = db.Column(db.String(20), nullable=False, default="draft")  # draft | completed | merged
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    # См. MovementDocument.synced_to_1c_at.
    synced_to_1c_at = db.Column(db.DateTime, nullable=True)
    # См. MovementDocument.accounting_entered_at — ручное исключение из
    # очереди выгрузки в 1С.
    accounting_entered_at = db.Column(db.DateTime, nullable=True)
    # Заполняется, когда несколько параллельных листов (по разным
    # людям/участкам склада) свели в один итоговый документ — см.
    # inventory.merge_documents. Статус такого листа становится "merged",
    # его короба и позиции остаются на месте как история подсчета.
    merged_into_id = db.Column(db.Integer, db.ForeignKey("inventory_documents.id"), nullable=True)
    # NULL — обычная (общая) инвентаризация по складу целиком, как раньше.
    # Заполнено — выборочная инвентаризация ОДНОЙ ячейки (см. чат): для
    # сравнения берется не весь учётный остаток склада, а только то, что
    # по системе сейчас физически стоит в этой ячейке (см.
    # inventory._cell_stock_by_nomenclature). Сканирование короба в таком
    # листе не только учитывает его в подсчете, но и сразу переставляет в
    # эту ячейку (см. inventory.add_box) — по сути инвентаризация ячейки
    # одновременно и есть ее фактическое размещение.
    cell_id = db.Column(db.Integer, db.ForeignKey("cells.id"), nullable=True)
    # Аналогично cell_id, но выборочная инвентаризация целого РЯДА без
    # ячеек (см. чат — помещения, где ячейки завести нельзя): сравнение
    # идет с тем, что стоит в рядy напрямую (Box.zone_id), а сканирование
    # короба сразу переставляет его в этот ряд (см. inventory.add_box).
    # Взаимоисключимо с cell_id — заполнено только одно из двух, либо ни одно.
    zone_id = db.Column(db.Integer, db.ForeignKey("zones.id"), nullable=True)

    warehouse = db.relationship("Warehouse")
    cell = db.relationship("Cell")
    zone = db.relationship("Zone")
    created_by = db.relationship("User")
    merged_into = db.relationship("InventoryDocument", remote_side=[id], backref="merged_from")
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


class ShipmentPlan(db.Model):
    """План отгрузок по маркетплейсу (ОЗОН/ВБ) — по одной строке на
    маркетплейс. Каждая новая загрузка файла полностью заменяет строки
    (ShipmentPlanLine) этого плана, сама запись плана переиспользуется."""

    __tablename__ = "shipment_plans"

    id = db.Column(db.Integer, primary_key=True)
    marketplace = db.Column(db.String(20), unique=True, nullable=False)  # "ozon" | "wb"
    sheet_name = db.Column(db.String(200))
    uploaded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    # Дата начала периода — разобрана из названия листа ("...от 27.08"),
    # см. utils.shipment_plan_import.extract_period_start. Пусто, если в
    # названии листа не нашлось даты. Период считается равным 14 дням.
    period_start = db.Column(db.Date, nullable=True)
    uploaded_by = db.relationship("User")
    lines = db.relationship(
        "ShipmentPlanLine", backref="plan", lazy="dynamic", cascade="all, delete-orphan"
    )


class ShipmentPlanLine(db.Model):
    """Одна позиция плана: сколько штук штрихкода X нужно отгрузить на
    склад-город Y. fulfilled_qty дописывается автоматически при проведении
    перемещения на этот склад (см. movement.complete) — не берется из файла."""

    __tablename__ = "shipment_plan_lines"

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("shipment_plans.id"), nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False, index=True)
    # Пусто, если штрихкод из плана не найден в номенклатуре — строка все
    # равно сохраняется, чтобы такие позиции было видно на дашборде.
    nomenclature_id = db.Column(db.Integer, db.ForeignKey("nomenclature.id"), nullable=True, index=True)
    barcode = db.Column(db.String(50), nullable=False)
    article = db.Column(db.String(200))
    size = db.Column(db.String(50))
    planned_qty = db.Column(db.Float, nullable=False, default=0)
    fulfilled_qty = db.Column(db.Float, nullable=False, default=0)
    # Дата берется из названия конкретного листа «Распределение». Поэтому
    # строки одного объединенного плана могут начинаться в разные даты.
    # Только перемещения с этой даты закрывают потребность строки.
    period_start = db.Column(db.Date, nullable=True)
    # Комментарий закупщиков из той же строки файла плана (см.
    # utils.shipment_plan_import._find_comment_col) — например, причина
    # задержки поставки конкретного SKU. Читается из Google/Excel заново
    # при каждой загрузке плана, как и planned_qty.
    buyer_comment = db.Column(db.Text)
    # Приоритет из колонки "Приоритет" файла плана (см. чат) — один и тот
    # же на все города/площадки этого штрихкода (колонка стоит до городов,
    # читается один раз на строку). Не участвует в выполнении плана — либо
    # красит строку товара в "Что нужно отправить" на дашборде, либо (для
    # значений 0/1/2) переключает распределение по городам на
    # distributed_target_qty вместо planned_qty (см. ниже).
    priority = db.Column(db.Integer, nullable=True)
    # Для приоритетных товаров (priority in (0, 1, 2)) — целевое количество
    # НА ЭТОТ ГОРОД, посчитанное при синхронизации плана как доля текущего
    # "готово к отгрузке" по штрихкоду пропорционально доле города в общем
    # плане (см. shipment_plan._apply_priority_distribution) — вместо
    # жесткого planned_qty города, если фактически упакованного товара
    # больше или меньше, чем весь план. NULL — приоритет не задан, либо
    # план по этому штрихкоду нулевой (не с чем считать долю).
    distributed_target_qty = db.Column(db.Float, nullable=True)

    warehouse = db.relationship("Warehouse")
    nomenclature = db.relationship("Nomenclature")

    __table_args__ = (
        db.UniqueConstraint("plan_id", "warehouse_id", "barcode", name="uq_plan_warehouse_barcode"),
    )

    def effective_planned_qty(self):
        """planned_qty этого города — либо, для приоритетных товаров с уже
        посчитанным распределением, пропорциональная цель по факту "готово
        к отгрузке" вместо жесткого плана города (см. distributed_target_qty
        и чат). Используется ТОЛЬКО подсказкой "куда везти короб" (см.
        movement._compute_routing) — сознательно не участвует в
        remaining_qty()/дашборде: там план и так уже показывает разрыв с
        планом по каждому городу, а при нулевом "готово к отгрузке" эта
        цель обнулилась бы и товар с реальной нехваткой пропал бы из
        "Что нужно отправить" вместо того чтобы показать проблему."""
        if self.priority in (0, 1, 2) and self.distributed_target_qty is not None:
            return self.distributed_target_qty
        return self.planned_qty

    def remaining_qty(self):
        fulfilled_qty = getattr(self, "current_fulfilled_qty", self.fulfilled_qty)
        return max(self.planned_qty - fulfilled_qty, 0)


class SupplierReturn(db.Model):
    """Возврат поставщику — брак, выделенный только на этапе разбраковки
    конкретной приемки. receiving_document_id связывает возврат с накладной
    и поставщиком для последующей выгрузки в 1С. Старые записи, созданные до
    введения этого правила, могут не иметь receiving_document_id.
    supplier_name/invoice_number — снимок на момент создания (как у
    UnplacedStockLot), чтобы отображение не менялось задним числом."""

    __tablename__ = "supplier_returns"

    id = db.Column(db.Integer, primary_key=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    nomenclature_id = db.Column(db.Integer, db.ForeignKey("nomenclature.id"), nullable=False)
    qty = db.Column(db.Float, nullable=False)
    comment = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    receiving_document_id = db.Column(
        db.Integer, db.ForeignKey("receiving_documents.id"), nullable=True
    )
    supplier_name = db.Column(db.String(200), nullable=True)
    # Номер приемки (ReceivingDocument.number) на момент создания возврата.
    # Совпадает с реальным номером накладной в 1С только для приемок,
    # загруженных из файла накладной (см. ReceivingDocument.invoice_file_name)
    # — для остальных 1С этот номер не найдет, см. SyncWMS.bsl.
    invoice_number = db.Column(db.String(30), nullable=True)
    # См. MovementDocument.synced_to_1c_at.
    synced_to_1c_at = db.Column(db.DateTime, nullable=True)
    # См. MovementDocument.accounting_entered_at — ручное исключение из
    # очереди выгрузки в 1С (проставляется сразу на все строки возврата по
    # одной приемке, см. integration_1c.toggle_supplier_return).
    accounting_entered_at = db.Column(db.DateTime, nullable=True)

    warehouse = db.relationship("Warehouse")
    nomenclature = db.relationship("Nomenclature")
    created_by = db.relationship("User")
    receiving_document = db.relationship("ReceivingDocument")


class OzonArticleMapping(db.Model):
    """Сопоставление штрихкода товара с артикулом, под которым он заведен в
    личном кабинете Ozon — нужно для выгрузки «Состав грузовых мест» (см.
    marketplace_export.export_ozon_package_composition): в файле для Ozon
    колонка «ШК товара» — это наш обычный Nomenclature.barcode, а вот
    «Артикул товара» у Ozon — отдельная строка, которая с нашим
    Nomenclature.sku не совпадает. Загружается отдельным файлом
    (см. marketplace_export.upload_ozon_mapping) — просто две колонки,
    штрихкод и артикул, без заголовка."""

    __tablename__ = "ozon_article_mappings"

    id = db.Column(db.Integer, primary_key=True)
    barcode = db.Column(db.String(50), unique=True, nullable=False, index=True)
    article = db.Column(db.String(200), nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


# Этапы производства до прихода на склад (после — уже ведется в WMS сквозь
# ReceivingDocument/PlacementDocument/MovementDocument) — см. панель
# руководителя и чат (уточненная схема). Порядок важен: используется и для
# последовательного "продвижения" этапа при синхронизации (см.
# production_orders._advance_stage), и для отображения воронки в нужном
# порядке.
#
# Точка отсчета первого этапа — НЕ отдельный статус в таблице, а ДАТА ИЗ
# НАЗВАНИЯ ЛИСТА (см. чат: "из таблицы берем дату из названия листа, это
# точка отсчета для поиска производства" — тот же прием, что уже есть для
# листов плана отгрузок, см. shipment_plan_import.extract_period_start),
# поэтому отдельного "заказ размещен" этапа здесь нет.
#
# "Отмена" и "переделка" образца — НЕ отдельные этапы воронки, а
# статус-модификаторы поверх "sample_sewing" (см. ProductionOrder.
# sample_cancelled_at/rework_count): переделка продлевает время в этом же
# этапе (просто возвращает current_stage на "sample_sewing", не трогая уже
# проставленные даты), отмена — терминальное состояние, заказ выбывает из
# расчета средних длительностей по живым заказам и считается отдельно.
PRODUCTION_ORDER_STAGE_KEYS = [
    "workshop_search",
    "sample_sewing",
    "sample_approved",
    "photo_requested",
    "mp_card_created",
    "data_in_1c",
    "order_in_1c",
]
PRODUCTION_ORDER_STAGE_LABELS = {
    "workshop_search": "Поиск поставщика/цеха",
    "sample_sewing": "Отшив образца",
    "sample_approved": "Образец согласован",
    "photo_requested": "Запрос фото образца",
    "mp_card_created": "Карточка на МП заведена",
    "data_in_1c": "Данные занесены в 1С",
    "order_in_1c": "Заказ внесен в 1С",
}
# Терминальное состояние — не часть линейной воронки выше (см. ProductionOrder.current_stage).
PRODUCTION_ORDER_STAGE_CANCELLED = "sample_cancelled"


class ProductionOrder(db.Model):
    """Заказ на пошив продукции — этапы ДО прихода на склад: поиск
    поставщика/цеха, отшив образца, согласование, запрос фото, карточка на
    МП, данные и заказ в 1С (см. чат — панель руководителя). После внесения
    заказа в 1С дальнейший ориентир — не статус из таблицы, а deadline_date
    (дедлайн партии) — дальше уже идет обычный процесс WMS (приемка и
    т.д., см. ReceivingDocument.order_number — сопоставляется с этим же
    order_number). Источник данных — внешняя Google-таблица, которую ведет
    менеджер вручную (см. production_orders.py — синхронизация по кнопке в
    самой таблице, по аналогии с планом отгрузок, см.
    shipment_plan.google_button_setup).

    Таблица хранит только ТЕКУЩИЙ статус заказа, без истории — сама WMS не
    может знать даты этапов, которые уже прошли ДО первой синхронизации.
    Если в таблице есть отдельные колонки с датами этапов (см.
    production_orders_import._STAGE_DATE_CANDIDATES), даты берутся из них
    напрямую — это надежный случай. Если таких колонок нет, WMS засчитывает
    дату этапа тем моментом, когда САМА впервые увидела эту строку в этом
    статусе (при каждой синхронизации) — это лишь приближение: если
    менеджер сменил статус за день до синхронизации (или вообще ни разу не
    запускал ее раньше), фактическая длительность этапа в отчете будет
    неточной. Чем чаще идет синхронизация, тем точнее."""

    __tablename__ = "production_orders"

    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(50), unique=True, nullable=False, index=True)
    marketplace = db.Column(db.String(20), nullable=True)
    # Текущий этап — один из PRODUCTION_ORDER_STAGE_KEYS, либо
    # PRODUCTION_ORDER_STAGE_CANCELLED, либо NULL, если текст статуса из
    # таблицы не удалось сопоставить ни с одним из них (см. raw_status —
    # тогда там видно, что именно не распозналось).
    current_stage = db.Column(db.String(30), nullable=True)
    raw_status = db.Column(db.String(200), nullable=True)

    workshop_search_started_at = db.Column(db.DateTime, nullable=True)
    sample_sewing_started_at = db.Column(db.DateTime, nullable=True)
    sample_approved_at = db.Column(db.DateTime, nullable=True)
    photo_requested_at = db.Column(db.DateTime, nullable=True)
    mp_card_created_at = db.Column(db.DateTime, nullable=True)
    data_in_1c_at = db.Column(db.DateTime, nullable=True)
    order_in_1c_at = db.Column(db.DateTime, nullable=True)

    # См. класс-докстринг и PRODUCTION_ORDER_STAGE_CANCELLED — не часть
    # линейной воронки, статус-модификаторы поверх "Отшив образца".
    sample_cancelled_at = db.Column(db.DateTime, nullable=True)
    rework_count = db.Column(db.Integer, nullable=False, default=0)
    last_rework_at = db.Column(db.DateTime, nullable=True)

    # Дедлайн партии из таблицы (одна дата на заказ, см. чат) — ориентир
    # ПОСЛЕ внесения заказа в 1С, когда дальше уже нет отдельных статусов
    # этой таблицы, а идет обычный процесс WMS. Обновляется при каждой
    # синхронизации (в отличие от дат этапов — дедлайн может сдвинуться
    # менеджером, а не только устанавливаться один раз).
    deadline_date = db.Column(db.Date, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_synced_at = db.Column(db.DateTime, nullable=True)

    _STAGE_TIMESTAMP_COLUMNS = {
        "workshop_search": "workshop_search_started_at",
        "sample_sewing": "sample_sewing_started_at",
        "sample_approved": "sample_approved_at",
        "photo_requested": "photo_requested_at",
        "mp_card_created": "mp_card_created_at",
        "data_in_1c": "data_in_1c_at",
        "order_in_1c": "order_in_1c_at",
    }

    def stage_timestamp(self, stage_key):
        column = self._STAGE_TIMESTAMP_COLUMNS.get(stage_key)
        return getattr(self, column) if column else None

    def set_stage_timestamp(self, stage_key, value):
        column = self._STAGE_TIMESTAMP_COLUMNS.get(stage_key)
        if column:
            setattr(self, column, value)
