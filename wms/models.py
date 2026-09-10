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

    def is_production_only(self):
        return self.role == "production" and not self.is_admin

    def allowed_section_set(self):
        if not self.allowed_sections:
            return set(SECTION_CODES)
        if self.allowed_sections == "none":
            return set()
        return set(self.allowed_sections.split(","))

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
    # физических складов оба поля пустые. marketplace_city хранит исходное
    # название города из файла плана, чтобы при повторной загрузке находить
    # тот же склад, а не плодить дубликаты каждые 2 недели.
    marketplace = db.Column(db.String(20), nullable=True)
    marketplace_city = db.Column(db.String(100), nullable=True)
    # Свободный текст (название организации/адрес/телефон) — печатается на
    # стикерах отправления (см. movement.export_shipping_labels) как
    # получатель этого склада-направления. Настраивается отдельно для
    # каждого склада, в т.ч. складов-городов маркетплейсов.
    recipient_info = db.Column(db.String(300), nullable=True)

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
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False, index=True)
    cell_id = db.Column(db.Integer, db.ForeignKey("cells.id"), nullable=True, index=True)
    placement_document_id = db.Column(
        db.Integer, db.ForeignKey("placement_documents.id"), nullable=True, index=True
    )
    status = db.Column(db.String(20), nullable=False, default="open")  # open | stored
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    warehouse = db.relationship("Warehouse", foreign_keys=[warehouse_id])
    items = db.relationship(
        "BoxItem", backref="box", lazy="dynamic", cascade="all, delete-orphan"
    )

    def total_qty(self):
        return sum(item.qty for item in self.items)

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
    status = db.Column(db.String(20), nullable=False, default="draft")  # draft | completed
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

    warehouse = db.relationship("Warehouse")
    created_by = db.relationship("User")
    supplier_ref = db.relationship("Supplier")
    lines = db.relationship(
        "ReceivingLine", backref="document", lazy="dynamic", cascade="all, delete-orphan"
    )

    def total_qty(self):
        return sum(line.qty for line in self.lines)


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

    nomenclature = db.relationship("Nomenclature")
    box = db.relationship("Box")


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
    status = db.Column(db.String(20), nullable=False, default="draft")  # draft | completed
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
    # Ручная отметка бухгалтера "внесено в 1С" — независима от synced_to_1c_at
    # выше (тот выставляется автоматически самой интеграцией, когда 1С
    # забирает документ через API). Бухгалтерия может вести учет отдельно
    # (например, вручную создавать документ в другой конфигурации) и просто
    # отмечает здесь галочкой сам факт для контроля, без завязки на API.
    accounting_entered_at = db.Column(db.DateTime, nullable=True)

    from_warehouse = db.relationship("Warehouse", foreign_keys=[from_warehouse_id])
    to_warehouse = db.relationship("Warehouse", foreign_keys=[to_warehouse_id])
    created_by = db.relationship("User")
    lines = db.relationship(
        "MovementLine", backref="document", lazy="dynamic", cascade="all, delete-orphan"
    )


class MovementReceiptDiscrepancy(db.Model):
    """Расхождение между тем, что отправлено (по коробам документа), и тем,
    что реально приняли на складе назначения — по одной строке на товар.
    Заполняется через "Принято с расхождением" (см. movement.receive_with_discrepancy)
    вместо обычной кнопки "Принято на складе", когда факт не совпадает —
    недостача или излишек. Обычная приемка без расхождений таких строк не
    создает вообще."""

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


class MovementLine(db.Model):
    """Один отсканированный короб в списке перемещения. from_* — снимок
    расположения короба на момент сканирования, to_cell_id — ячейка
    назначения на складе документа (можно указать сразу или позже, до
    завершения документа)."""

    __tablename__ = "movement_lines"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("movement_documents.id"), nullable=False, index=True)
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
    status = db.Column(db.String(20), nullable=False, default="draft")  # draft | completed | merged
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    # См. MovementDocument.synced_to_1c_at.
    synced_to_1c_at = db.Column(db.DateTime, nullable=True)
    # Заполняется, когда несколько параллельных листов (по разным
    # людям/участкам склада) свели в один итоговый документ — см.
    # inventory.merge_documents. Статус такого листа становится "merged",
    # его короба и позиции остаются на месте как история подсчета.
    merged_into_id = db.Column(db.Integer, db.ForeignKey("inventory_documents.id"), nullable=True)

    warehouse = db.relationship("Warehouse")
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

    warehouse = db.relationship("Warehouse")
    nomenclature = db.relationship("Nomenclature")

    __table_args__ = (
        db.UniqueConstraint("plan_id", "warehouse_id", "barcode", name="uq_plan_warehouse_barcode"),
    )

    def remaining_qty(self):
        return max(self.planned_qty - self.fulfilled_qty, 0)


class SupplierReturn(db.Model):
    """Списание брака с неразмещенного остатка через возврат поставщику.
    Сам документ возврата оформляется в 1С отдельно — здесь только
    фиксируем количество и товар, чтобы было с чем сверить 1С-документ."""

    __tablename__ = "supplier_returns"

    id = db.Column(db.Integer, primary_key=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    nomenclature_id = db.Column(db.Integer, db.ForeignKey("nomenclature.id"), nullable=False)
    qty = db.Column(db.Float, nullable=False)
    comment = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    warehouse = db.relationship("Warehouse")
    nomenclature = db.relationship("Nomenclature")
    created_by = db.relationship("User")
