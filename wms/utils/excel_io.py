import io
from datetime import datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .categorize import classify_by_name

NOMENCLATURE_HEADERS = [
    "Штрихкод",
    "Наименование",
    "Размер",
    "Ед. изм.",
    "Описание",
    "Норма времени на 1 шт, мин",
    "Артикул (необязательно)",
]


def _style_header(ws, headers):
    fill = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")
    for idx, title in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=idx, value=title)
        cell.font = Font(bold=True)
        cell.fill = fill
        ws.column_dimensions[get_column_letter(idx)].width = max(16, len(title) + 4)


def build_nomenclature_template() -> bytes:
    """Готовит xlsx-шаблон для заполнения номенклатуры."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Номенклатура"
    _style_header(ws, NOMENCLATURE_HEADERS)

    example = ["4600000000015", "Пример: Футболка белая", "XL", "шт", "", 12, ""]
    ws.append(example)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


class ImportResult:
    def __init__(self):
        self.created = 0
        self.updated = 0
        self.errors = []  # list[str]

    @property
    def total(self):
        return self.created + self.updated


def import_nomenclature_from_excel(file_stream, db, Nomenclature) -> ImportResult:
    """Импортирует/обновляет номенклатуру из xlsx-файла (по шаблону)."""
    result = ImportResult()

    try:
        # file_stream от Flask (request.files[...].stream) на некоторых
        # версиях Python — SpooledTemporaryFile без метода seekable(),
        # который требует openpyxl (через zipfile). Перекладываем в
        # BytesIO, который всегда полноценно seekable, вне зависимости от
        # версии Python и от того, ушла ли загрузка на диск.
        wb = load_workbook(io.BytesIO(file_stream.read()), data_only=True)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"Не удалось открыть файл: {exc}")
        return result

    ws = wb.active

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue

        barcode = str(row[0]).strip() if len(row) > 0 and row[0] is not None else ""
        name = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
        size = str(row[2]).strip() if len(row) > 2 and row[2] is not None else ""
        unit = str(row[3]).strip() if len(row) > 3 and row[3] not in (None, "") else "шт"
        description = str(row[4]).strip() if len(row) > 4 and row[4] is not None else ""
        norm_minutes = None
        if len(row) > 5 and row[5] not in (None, ""):
            try:
                norm_minutes = float(row[5])
            except (TypeError, ValueError):
                result.errors.append(f"Строка {row_idx}: некорректная норма времени '{row[5]}'")
        # Артикул необязателен — если не указан, берется равным штрихкоду
        # (уникальный, всегда есть). Основной идентификатор товара для этой
        # компании — именно штрихкод, артикул часто просто отсутствует.
        sku = str(row[6]).strip() if len(row) > 6 and row[6] is not None else ""

        if not barcode or not name:
            result.errors.append(f"Строка {row_idx}: не заполнен штрихкод или наименование")
            continue

        if not sku:
            sku = barcode

        existing = Nomenclature.query.filter_by(barcode=barcode).first()

        sku_owner = Nomenclature.query.filter_by(sku=sku).first()
        if sku_owner is not None and (existing is None or sku_owner.id != existing.id):
            result.errors.append(
                f"Строка {row_idx}: артикул '{sku}' уже используется другим товаром"
            )
            continue

        if existing:
            existing.sku = sku
            existing.name = name
            existing.size = size or None
            existing.unit = unit or "шт"
            existing.description = description
            if norm_minutes is not None:
                existing.norm_minutes = norm_minutes
            # Вид товара переопределять не трогаем, если он уже выставлен
            # (вручную или предыдущим импортом) — только если пуст.
            if existing.category_id is None:
                category = classify_by_name(name)
                existing.category_id = category.id if category else None
            result.updated += 1
        else:
            category = classify_by_name(name)
            item = Nomenclature(
                sku=sku,
                barcode=barcode,
                name=name,
                size=size or None,
                unit=unit or "шт",
                description=description,
                norm_minutes=norm_minutes,
                category_id=category.id if category else None,
            )
            db.session.add(item)
            result.created += 1

    return result


NOMENCLATURE_EXPORT_HEADERS = NOMENCLATURE_HEADERS + ["Остаток"]


def export_nomenclature_to_excel(items, stock_by_item=None) -> bytes:
    """stock_by_item — {nomenclature_id: кол-во} (см.
    nomenclature._stock_by_item) — сумма упакованного в короба и
    неразмещенного остатка. Отдельные от NOMENCLATURE_HEADERS заголовки,
    т.к. эта же константа используется и для шаблона ЗАГРУЗКИ номенклатуры
    (build_nomenclature_template), где колонки остатка быть не должно —
    остаток не то, что можно "загрузить" при создании товара."""
    stock_by_item = stock_by_item or {}
    wb = Workbook()
    ws = wb.active
    ws.title = "Номенклатура"
    _style_header(ws, NOMENCLATURE_EXPORT_HEADERS)
    for item in items:
        ws.append(
            [
                item.barcode,
                item.name,
                item.size or "",
                item.unit,
                item.description or "",
                item.norm_minutes or "",
                item.sku,
                stock_by_item.get(item.id, 0),
            ]
        )
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


RECEIVING_HEADERS = [
    "Номер документа",
    "Дата",
    "Склад",
    "Поставщик",
    "Статус",
    "Артикул",
    "Штрихкод",
    "Наименование",
    "Кол-во",
    "Ед. изм.",
]


def export_receiving_to_excel(documents) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Приемка"
    _style_header(ws, RECEIVING_HEADERS)

    status_map = {"draft": "Черновик", "completed": "Завершен"}

    for doc in documents:
        for line in doc.lines:
            ws.append(
                [
                    doc.number,
                    doc.created_at.strftime("%Y-%m-%d %H:%M") if doc.created_at else "",
                    doc.warehouse.name if doc.warehouse else "",
                    doc.supplier or "",
                    status_map.get(doc.status, doc.status),
                    line.nomenclature.sku if line.nomenclature else "",
                    line.nomenclature.barcode if line.nomenclature else "",
                    line.nomenclature.name if line.nomenclature else "",
                    line.qty,
                    line.nomenclature.unit if line.nomenclature else "",
                ]
            )

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


PLACEMENT_HEADERS = [
    "Номер документа",
    "Дата",
    "Склад",
    "Статус",
    "Артикул",
    "Штрихкод",
    "Наименование",
    "Кол-во",
    "Ед. изм.",
    "Короб",
    "Ячейка",
]


def export_placement_to_excel(documents) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Размещение"
    _style_header(ws, PLACEMENT_HEADERS)

    status_map = {"draft": "Черновик", "completed": "Завершен"}

    for doc in documents:
        for line in doc.lines:
            ws.append(
                [
                    doc.number,
                    doc.created_at.strftime("%Y-%m-%d %H:%M") if doc.created_at else "",
                    doc.warehouse.name if doc.warehouse else "",
                    status_map.get(doc.status, doc.status),
                    line.nomenclature.sku if line.nomenclature else "",
                    line.nomenclature.barcode if line.nomenclature else "",
                    line.nomenclature.name if line.nomenclature else "",
                    line.qty,
                    line.nomenclature.unit if line.nomenclature else "",
                    line.box.box_number if line.box else "",
                    line.box.cell.code if (line.box and line.box.cell) else "",
                ]
            )

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


MOVEMENT_HEADERS = [
    "Номер документа",
    "Дата",
    "Статус",
    "Короб",
    "Артикул",
    "Штрихкод",
    "Наименование",
    "Кол-во",
    "Ед. изм.",
    "Склад-источник",
    "Ячейка-источник",
    "Склад-назначение",
    "Ячейка-назначение",
]


def export_movement_to_excel(documents) -> bytes:
    """Одна строка на каждый товар в каждом коробе документа перемещения —
    короб сканируется целиком, но в отчете видно содержимое."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Перемещения"
    _style_header(ws, MOVEMENT_HEADERS)

    status_map = {"draft": "Черновик", "completed": "Завершен"}

    for doc in documents:
        for line in doc.lines:
            box_items = list(line.box.items) if line.box else []
            rows = box_items or [None]
            for box_item in rows:
                ws.append(
                    [
                        doc.number,
                        doc.created_at.strftime("%Y-%m-%d %H:%M") if doc.created_at else "",
                        status_map.get(doc.status, doc.status),
                        line.box.box_number if line.box else "",
                        box_item.nomenclature.sku if box_item else "",
                        box_item.nomenclature.barcode if box_item else "",
                        box_item.nomenclature.name if box_item else "",
                        box_item.qty if box_item else "",
                        box_item.nomenclature.unit if box_item else "",
                        line.from_warehouse.name if line.from_warehouse else "",
                        line.from_cell.code if line.from_cell else "",
                        doc.to_warehouse.name if doc.to_warehouse else "",
                        line.to_cell.code if line.to_cell else "",
                    ]
                )

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


MOVEMENT_SUMMARY_HEADERS = [
    "Номер документа",
    "Дата",
    "Статус",
    "Склад-источник",
    "Склад-назначение",
    "№ заявки МП",
    "Кол-во коробов",
    "Кол-во товара",
]


def export_movement_summary_to_excel(documents) -> bytes:
    """Одна строка на документ перемещения целиком (в отличие от
    export_movement_to_excel, где строка на каждый товар в каждом коробе) —
    только количество коробов и суммарное количество товара, для быстрой
    сверки объемов без разбора по позициям."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Перемещения (сводно)"
    _style_header(ws, MOVEMENT_SUMMARY_HEADERS)

    status_map = {"draft": "Черновик", "completed": "Завершен", "merged": "Объединен"}

    for doc in documents:
        ws.append(
            [
                doc.number,
                doc.created_at.strftime("%Y-%m-%d %H:%M") if doc.created_at else "",
                status_map.get(doc.status, doc.status),
                doc.from_warehouse.name if doc.from_warehouse else "",
                doc.to_warehouse.name if doc.to_warehouse else "",
                doc.marketplace_request_number or "",
                doc.lines.count(),
                doc.total_item_qty(),
            ]
        )

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


PRODUCTION_HEADERS = [
    "Дата",
    "Сотрудник",
    "Кол-во, шт",
    "Нормо-минуты",
    "Плановая смена, мин",
    "Эффективность, %",
    "Из них без нормы, шт",
]


def export_production_to_excel(rows) -> bytes:
    """rows — список словарей из production._efficiency_rows()."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Эффективность"
    _style_header(ws, PRODUCTION_HEADERS)

    for row in rows:
        ws.append(
            [
                row["work_date"].strftime("%Y-%m-%d") if row["work_date"] else "",
                row["user"].display_name() if row["user"] else "",
                row["qty"],
                row["normo_minutes"],
                row["shift_minutes"],
                row["efficiency"] if row["efficiency"] is not None else "",
                row["missing_norm"],
            ]
        )

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


INVENTORY_HEADERS = [
    "Номер документа",
    "Дата",
    "Склад",
    "Статус",
    "Артикул",
    "Штрихкод",
    "Наименование",
    "Кол-во",
    "Ед. изм.",
]


def export_inventory_to_excel(documents) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Инвентаризация"
    _style_header(ws, INVENTORY_HEADERS)

    status_map = {"draft": "Черновик", "completed": "Завершен"}

    for doc in documents:
        for line in doc.lines:
            ws.append(
                [
                    doc.number,
                    doc.created_at.strftime("%Y-%m-%d %H:%M") if doc.created_at else "",
                    doc.warehouse.name if doc.warehouse else "",
                    status_map.get(doc.status, doc.status),
                    line.nomenclature.sku if line.nomenclature else "",
                    line.nomenclature.barcode if line.nomenclature else "",
                    line.nomenclature.name if line.nomenclature else "",
                    line.qty,
                    line.nomenclature.unit if line.nomenclature else "",
                ]
            )

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def export_shipment_plan_to_excel(picking_list, picking_totals, ozon_cities, wb_cities) -> bytes:
    """Выгружает ту же сводную таблицу, которая показана на дашборде.

    Общие показатели товара идут слева, затем отдельные цветовые блоки
    городов ОЗОН и ВБ. Остаток плана и количество в пути не складываются:
    при наличии отправленного товара ячейка города выглядит как «25 (10)».
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "План отгрузок"
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 85
    ws.freeze_panes = "H4"
    ws.print_options.horizontalCentered = False
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    common_headers = [
        "Артикул",
        "Размер",
        "Штрихкод",
        "",
        "На разбраковке",
        "Готово к отгрузке",
        "В пути",
    ]
    ozon_fill = PatternFill("solid", fgColor="D9EEF7")
    wb_fill = PatternFill("solid", fgColor="FFF2CC")
    total_fill = PatternFill("solid", fgColor="E2E3E5")
    no_stock_fill = PatternFill("solid", fgColor="F4CCCC")
    badge_fill = PatternFill("solid", fgColor="DC3545")
    thin_gray = Side(style="thin", color="D9D9D9")
    medium_gray = Side(style="medium", color="A6A6A6")

    for column, title in enumerate(common_headers, start=1):
        ws.merge_cells(start_row=1, start_column=column, end_row=2, end_column=column)
        cell = ws.cell(1, column, title)
        cell.font = Font(name="Arial", size=10, bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    city_columns = {}
    next_column = len(common_headers) + 1
    for marketplace, label, cities, fill in (
        ("ozon", "ОЗОН", ozon_cities, ozon_fill),
        ("wb", "ВБ", wb_cities, wb_fill),
    ):
        if not cities:
            continue
        start_column = next_column
        end_column = start_column + len(cities) - 1
        ws.merge_cells(start_row=1, start_column=start_column, end_row=1, end_column=end_column)
        group_cell = ws.cell(1, start_column, label)
        group_cell.font = Font(name="Arial", size=10, bold=True)
        group_cell.alignment = Alignment(horizontal="center", vertical="center")
        for offset, city in enumerate(cities):
            column = start_column + offset
            city_columns[(marketplace, city)] = column
            cell = ws.cell(2, column, city)
            cell.font = Font(name="Arial", size=9, bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(column)].width = max(12, min(len(city) + 3, 20))
        for row in (1, 2):
            for column in range(start_column, end_column + 1):
                ws.cell(row, column).fill = fill
        next_column = end_column + 1

    last_column = max(next_column - 1, len(common_headers))
    for row in (1, 2):
        for column in range(1, last_column + 1):
            cell = ws.cell(row, column)
            cell.border = Border(bottom=medium_gray)

    total_row = 3
    ws.cell(total_row, 1, f"Итого ({len(picking_list)} поз.)")
    ws.cell(total_row, 5, picking_totals["unplaced"])
    ws.cell(total_row, 6, picking_totals["ready_to_ship"])
    ws.cell(total_row, 7, picking_totals["in_transit"])
    for (marketplace, city), column in city_columns.items():
        ws.cell(total_row, column, picking_totals[marketplace][city])
    for column in range(1, last_column + 1):
        cell = ws.cell(total_row, column)
        cell.fill = total_fill
        cell.font = Font(name="Arial", size=9, bold=True)
        cell.alignment = Alignment(horizontal="right" if column >= 5 else "left", vertical="center")
        cell.border = Border(bottom=medium_gray)
        if column >= 5:
            cell.number_format = '#,##0;-#,##0;—'

    for row_number, product in enumerate(picking_list, start=4):
        ws.cell(row_number, 1, product["article"])
        ws.cell(row_number, 2, product["size"])
        ws.cell(row_number, 3, product["barcode"])
        ws.cell(row_number, 4, "нет на складе" if product["no_stock"] else "")
        ws.cell(row_number, 5, product["unplaced"])
        ws.cell(row_number, 6, product["ready_to_ship"])
        ws.cell(row_number, 7, product["in_transit_total"])

        for (marketplace, city), column in city_columns.items():
            line = product[marketplace].get(city)
            if not line:
                value = None
            elif line.in_transit_qty:
                value = f"{int(line.effective_remaining_qty)} ({int(line.in_transit_qty)})"
            else:
                value = line.effective_remaining_qty
            ws.cell(row_number, column, value)

        for column in range(1, last_column + 1):
            cell = ws.cell(row_number, column)
            cell.font = Font(name="Arial", size=9)
            cell.alignment = Alignment(
                horizontal="right" if column >= 5 else "left",
                vertical="center",
            )
            cell.border = Border(bottom=thin_gray)
            if product["no_stock"]:
                cell.fill = no_stock_fill
            if column >= 5 and not isinstance(cell.value, str):
                cell.number_format = '#,##0;-#,##0;—'

        if product["no_stock"]:
            badge = ws.cell(row_number, 4)
            badge.fill = badge_fill
            badge.font = Font(name="Arial", size=8, bold=True, color="FFFFFF")
            badge.alignment = Alignment(horizontal="center", vertical="center")

    widths = {1: 30, 2: 12, 3: 19, 4: 16, 5: 18, 6: 22, 7: 12}
    for column, width in widths.items():
        ws.column_dimensions[get_column_letter(column)].width = width
    ws.row_dimensions[1].height = 20
    ws.row_dimensions[2].height = 32
    ws.row_dimensions[3].height = 21

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


SHIPPED_REPORT_HEADERS = [
    "Склад назначения",
    "Штрихкод",
    "Наименование",
    "Артикул",
    "Кол-во отгружено",
]


def export_shipped_report_to_excel(rows) -> bytes:
    """rows — список словарей {"warehouse", "nomenclature", "qty"} (см.
    reports.shipped_report)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Отгружено по складам"
    _style_header(ws, SHIPPED_REPORT_HEADERS)

    for row in rows:
        nomenclature = row["nomenclature"]
        ws.append(
            [
                row["warehouse"].name if row["warehouse"] else "",
                nomenclature.barcode if nomenclature else "",
                nomenclature.name if nomenclature else "— нет в номенклатуре —",
                nomenclature.sku if nomenclature else "",
                row["qty"],
            ]
        )

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


RECEIVING_STATUS_REPORT_HEADERS = [
    "Номер",
    "Склад",
    "Статус",
    "Дней в статусе",
    "Кол-во товара",
    "Вид товара",
]


def export_receiving_status_report_to_excel(rows) -> bytes:
    """rows — список словарей {"document", "status_label", "days_in_status",
    "qty", "categories"} (см. reports._receiving_status_rows)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Статусы приемки"
    _style_header(ws, RECEIVING_STATUS_REPORT_HEADERS)

    for row in rows:
        doc = row["document"]
        ws.append(
            [
                doc.number,
                doc.warehouse.name if doc.warehouse else "",
                row["status_label"],
                row["days_in_status"],
                row["qty"],
                row["categories"],
            ]
        )

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def timestamp_for_filename() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# Заголовки — дословно как в официальных шаблонах маркетплейсов (см.
# marketplace_export.py), чтобы файл принимался без ручной правки колонок.
OZON_PACKAGE_HEADERS = [
    "ШК товара",
    "Артикул товара",
    "Кол-во товаров",
    "Зона размещения",
    "Срок годности ДО в формате YYYY-MM-DD (не более 1 СГ на 1 SKU в 1 ГМ)",
    "ШК ГМ",
    "Тип ГМ (не обязательно)",
]

# Второй лист файла "Состав ГМ поставки" (см. export_ozon_package_composition)
# — не часть официального шаблона Ozon, просто наше собственное
# сопоставление короба и грузоместа для контроля при сборке/сверке.
OZON_PACKAGE_MAPPING_HEADERS = ["Наш короб", "Грузоместо Ozon", "Номенклатура", "Кол-во"]

WB_PACKAGE_HEADERS = [
    "Баркод товара",
    "Кол-во товаров",
    "ШК короба",
    "Срок годности",
    "ШК короба для печати в стороннем сервисе",
]


def export_ozon_package_composition(rows) -> bytes:
    """rows — [{"barcode", "article", "qty", "cargo_barcode", "box_number",
    "name"}], одна строка на товар в одном грузовом месте (см.
    marketplace_export.ozon_package_composition). Срок годности и зона
    размещения WMS не отслеживает — оставляются пустыми, заполняются
    вручную при необходимости, как и предусматривает сам шаблон Ozon.

    Второй лист ("Наши короба и ГМ") — не часть официального шаблона Ozon,
    просто сопоставление нашего короба (Box.box_number) и присвоенного ему
    грузоместа Ozon для собственного контроля при сборке/сверке поставки."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Состав ГМ поставки"
    _style_header(ws, OZON_PACKAGE_HEADERS)

    for row in rows:
        ws.append(
            [
                row["barcode"],
                row["article"],
                row["qty"],
                "",
                "",
                row["cargo_barcode"],
                "Коробка",
            ]
        )

    ws2 = wb.create_sheet("Наши короба и ГМ")
    _style_header(ws2, OZON_PACKAGE_MAPPING_HEADERS)
    for row in rows:
        ws2.append([row["box_number"], row["cargo_barcode"], row["name"], row["qty"]])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def export_wb_package_composition(rows) -> bytes:
    """rows — [{"barcode", "qty", "box_barcode"}], одна строка на товар в
    одном коробе (см. marketplace_export.wb_package_composition)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    _style_header(ws, WB_PACKAGE_HEADERS)

    for row in rows:
        ws.append([row["barcode"], row["qty"], row["box_barcode"], "", ""])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


WB_SUPPLY_REQUEST_HEADERS = ["Баркод", "Количество"]


def export_wb_supply_request(rows) -> bytes:
    """rows — [{"barcode", "qty"}], одна строка на баркод с суммарным
    количеством по ВСЕМ коробам перемещения сразу (см.
    marketplace_export.wb_supply_request) — второй, более простой файл для
    WB, отдельно от подробного состава по коробам
    (export_wb_package_composition)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    _style_header(ws, WB_SUPPLY_REQUEST_HEADERS)

    for row in rows:
        ws.append([row["barcode"], row["qty"]])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# Заголовки шаблона Ozon "заявка на поставку" (products-import-template) —
# именно в нижнем регистре, как в самом шаблоне.
OZON_SUPPLY_REQUEST_HEADERS = ["артикул", "имя (необязательно)", "количество"]


def export_ozon_supply_request(rows) -> bytes:
    """rows — [{"article", "name", "qty"}], одна строка на SKU с суммарным
    количеством по ВСЕМ коробам перемещения (см.
    marketplace_export.ozon_supply_request) — это заявка на поставку
    целиком, отдельно от состава по грузовым местам
    (export_ozon_package_composition): сумма количеств в обоих файлах
    должна совпадать, иначе Ozon аннулирует состав ГМ."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    _style_header(ws, OZON_SUPPLY_REQUEST_HEADERS)

    for row in rows:
        ws.append([row["article"], row["name"], row["qty"]])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
