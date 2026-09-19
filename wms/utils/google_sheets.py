"""Прямой обмен WMS с Google Таблицей плана отгрузок."""

import io
import os
from collections import defaultdict
from datetime import datetime

from openpyxl import Workbook

from ..models import MovementDocument, Nomenclature, ShipmentPlanLine


OUTPUT_SHEET_TITLE = "WMS — перемещения"
OUTPUT_HEADERS = [
    "Обновлено WMS",
    "Маркетплейс",
    "Склад назначения",
    "Штрихкод",
    "Артикул",
    "Товар",
    "В пути",
    "Принято",
    "Всего отгружено",
]


def google_sheets_configured(app):
    return bool(
        app.config.get("GOOGLE_SHEETS_SPREADSHEET_ID")
        and os.path.isfile(app.config.get("GOOGLE_SERVICE_ACCOUNT_FILE", ""))
    )


def distribution_sheet_titles(sheet_titles):
    """Все листы с признаком «Распределение», без ограничения по дате."""
    return [title for title in sheet_titles if "распределение" in title.casefold()]


def _service(app):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Не установлены библиотеки Google API") from exc

    credentials = service_account.Credentials.from_service_account_file(
        app.config["GOOGLE_SERVICE_ACCOUNT_FILE"],
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _a1_sheet(title):
    return "'" + title.replace("'", "''") + "'"


def load_distribution_workbook(app):
    """Читает значения подходящих листов через Sheets API и собирает
    совместимую с существующим импортом книгу в памяти."""
    if not google_sheets_configured(app):
        raise RuntimeError("Google Таблица не настроена")
    service = _service(app)
    spreadsheet_id = app.config["GOOGLE_SHEETS_SPREADSHEET_ID"]
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties(title,index)",
    ).execute()
    titles = distribution_sheet_titles(
        [s["properties"]["title"] for s in metadata.get("sheets", [])]
    )
    if not titles:
        raise RuntimeError("Нет листов с признаком «Распределение»")

    response = service.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=[_a1_sheet(title) for title in titles],
        majorDimension="ROWS",
        valueRenderOption="UNFORMATTED_VALUE",
        dateTimeRenderOption="FORMATTED_STRING",
    ).execute()

    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, value_range in zip(titles, response.get("valueRanges", [])):
        worksheet = workbook.create_sheet(title=title)
        for row in value_range.get("values", []):
            worksheet.append(row)
    stream = io.BytesIO()
    workbook.save(stream)
    stream.seek(0)
    return stream, titles


def _movement_totals():
    totals = defaultdict(
        lambda: {
            "in_transit": 0.0,
            "received": 0.0,
            "warehouse": None,
            "nomenclature": None,
        }
    )
    documents = MovementDocument.query.filter_by(status="completed").all()
    for document in documents:
        warehouse = document.to_warehouse
        if not warehouse.marketplace:
            continue

        expected = defaultdict(float)
        nomenclature_by_id = {}
        for line in document.lines:
            for item in line.box.items:
                expected[item.nomenclature_id] += item.qty
                nomenclature_by_id[item.nomenclature_id] = item.nomenclature

        actual = dict(expected)
        if document.received_at is not None:
            for discrepancy in document.discrepancies:
                actual[discrepancy.nomenclature_id] = discrepancy.received_qty

        for nomenclature_id, expected_qty in expected.items():
            nomenclature = nomenclature_by_id.get(nomenclature_id) or Nomenclature.query.get(
                nomenclature_id
            )
            if nomenclature is None:
                continue
            key = (warehouse.id, nomenclature.id)
            totals[key]["warehouse"] = warehouse
            totals[key]["nomenclature"] = nomenclature
            if document.received_at is None:
                totals[key]["in_transit"] += expected_qty
            else:
                totals[key]["received"] += actual.get(nomenclature_id, expected_qty)
    return totals


def received_wms_totals():
    """Фактически принятые количества по складу и товару."""
    return {key: value["received"] for key, value in _movement_totals().items()}


def build_wms_movement_rows():
    """Агрегирует только факт WMS. Повторный экспорт всегда дает тот же
    результат, поэтому сетевой повтор не способен задвоить количество."""
    totals = _movement_totals()

    # Если в плане есть более подходящий артикул, используем его вместо
    # внутреннего SKU номенклатуры.
    article_by_key = {
        (line.plan.marketplace, line.warehouse.marketplace_city, line.barcode): line.article
        for line in ShipmentPlanLine.query.all()
        if line.article
    }
    updated_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    rows = []
    sortable = []
    for quantities in totals.values():
        warehouse = quantities["warehouse"]
        nomenclature = quantities["nomenclature"]
        sortable.append(
            (
                warehouse.marketplace,
                warehouse.marketplace_city or warehouse.name,
                nomenclature.barcode or "",
                quantities,
            )
        )
    for marketplace, city, barcode, quantities in sorted(sortable, key=lambda row: row[:3]):
        nomenclature = quantities["nomenclature"]
        sku, name = nomenclature.sku or "", nomenclature.name
        article = article_by_key.get((marketplace, city, barcode)) or sku
        in_transit = quantities["in_transit"]
        received = quantities["received"]
        rows.append(
            [
                updated_at,
                marketplace.upper(),
                city,
                barcode,
                article,
                name,
                in_transit,
                received,
                in_transit + received,
            ]
        )
    return rows


def _ensure_output_sheet(service, spreadsheet_id):
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties(sheetId,title,gridProperties(rowCount))",
    ).execute()
    for sheet in metadata.get("sheets", []):
        if sheet["properties"]["title"] == OUTPUT_SHEET_TITLE:
            return sheet["properties"]
    response = service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": OUTPUT_SHEET_TITLE,
                            "gridProperties": {"frozenRowCount": 1},
                        }
                    }
                }
            ]
        },
    ).execute()
    return response["replies"][0]["addSheet"]["properties"]


def _ensure_output_row_capacity(service, spreadsheet_id, properties, required_rows):
    row_count = properties.get("gridProperties", {}).get("rowCount", 1000)
    if required_rows <= row_count:
        return row_count
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": properties["sheetId"],
                            "gridProperties": {"rowCount": required_rows},
                        },
                        "fields": "gridProperties.rowCount",
                    }
                }
            ]
        },
    ).execute()
    return required_rows


def write_wms_movement_sheet(app):
    service = _service(app)
    spreadsheet_id = app.config["GOOGLE_SHEETS_SPREADSHEET_ID"]
    properties = _ensure_output_sheet(service, spreadsheet_id)
    rows = build_wms_movement_rows()
    values = [OUTPUT_HEADERS] + rows
    row_count = _ensure_output_row_capacity(
        service, spreadsheet_id, properties, len(values)
    )
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{_a1_sheet(OUTPUT_SHEET_TITLE)}!A1:I{len(values)}",
        valueInputOption="RAW",
        body={"values": values},
    ).execute()
    # Старые хвостовые строки очищаем только после успешной записи новых.
    if len(values) < row_count:
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=(
                f"{_a1_sheet(OUTPUT_SHEET_TITLE)}!"
                f"A{len(values) + 1}:I{row_count}"
            ),
            body={},
        ).execute()
    return len(rows)
