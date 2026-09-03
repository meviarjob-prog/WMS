import io
from datetime import datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

NOMENCLATURE_HEADERS = [
    "Артикул (SKU)",
    "Штрихкод",
    "Наименование",
    "Ед. изм.",
    "Описание",
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

    example = ["ART-0001", "4600000000015", "Пример: Футболка белая XL", "шт", ""]
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
        wb = load_workbook(file_stream, data_only=True)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"Не удалось открыть файл: {exc}")
        return result

    ws = wb.active

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue

        sku = str(row[0]).strip() if len(row) > 0 and row[0] is not None else ""
        barcode = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
        name = str(row[2]).strip() if len(row) > 2 and row[2] is not None else ""
        unit = str(row[3]).strip() if len(row) > 3 and row[3] not in (None, "") else "шт"
        description = str(row[4]).strip() if len(row) > 4 and row[4] is not None else ""

        if not sku or not name:
            result.errors.append(f"Строка {row_idx}: не заполнен артикул или наименование")
            continue

        if not barcode:
            barcode = sku

        existing = Nomenclature.query.filter_by(sku=sku).first()

        barcode_owner = Nomenclature.query.filter_by(barcode=barcode).first()
        if barcode_owner is not None and (existing is None or barcode_owner.id != existing.id):
            result.errors.append(
                f"Строка {row_idx}: штрихкод '{barcode}' уже используется другим товаром"
            )
            continue

        if existing:
            existing.barcode = barcode
            existing.name = name
            existing.unit = unit or "шт"
            existing.description = description
            result.updated += 1
        else:
            item = Nomenclature(
                sku=sku,
                barcode=barcode,
                name=name,
                unit=unit or "шт",
                description=description,
            )
            db.session.add(item)
            result.created += 1

    return result


def export_nomenclature_to_excel(items) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Номенклатура"
    _style_header(ws, NOMENCLATURE_HEADERS)
    for item in items:
        ws.append([item.sku, item.barcode, item.name, item.unit, item.description or ""])
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
    "Короб",
    "Ячейка",
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
    "Короб",
    "Склад-источник",
    "Ячейка-источник",
    "Склад-назначение",
    "Ячейка-назначение",
]


def export_movement_to_excel(documents) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Перемещения"
    _style_header(ws, MOVEMENT_HEADERS)

    for doc in documents:
        ws.append(
            [
                doc.number,
                doc.created_at.strftime("%Y-%m-%d %H:%M") if doc.created_at else "",
                doc.box.box_number if doc.box else "",
                doc.from_warehouse.name if doc.from_warehouse else "",
                doc.from_cell.code if doc.from_cell else "",
                doc.to_warehouse.name if doc.to_warehouse else "",
                doc.to_cell.code if doc.to_cell else "",
            ]
        )

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def timestamp_for_filename() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")
