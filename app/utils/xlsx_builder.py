"""Генерация Excel-таблицы (.xlsx) по вызову инструмента generate_spreadsheet.

Зеркалит app/utils/pptx_builder.py: модель отдаёт структурированные данные
(листы -> заголовки колонок + строки), мы собираем реальный .xlsx через
openpyxl и возвращаем байты (загрузка в R2 — в app/tools).
"""

import io

from openpyxl import Workbook

from app.utils.tool_schema import SUMMARY_PROP

SPREADSHEET_TOOL = {
    "name": "generate_spreadsheet",
    "description": (
        "Generates an Excel spreadsheet (.xlsx) from tabular data and "
        "delivers it as a downloadable attachment. Use this when the user asks for an "
        "export, a data file, or a table they explicitly want to download "
        "— NOT for small tables that are more useful shown directly in "
        "the chat reply."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary": SUMMARY_PROP,
            "filename": {
                "type": "string",
                "description": "Base filename for the spreadsheet, without extension.",
            },
            "sheets": {
                "type": "array",
                "description": "One or more sheets to include, in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Sheet tab name."},
                        "headers": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Column headers, in order.",
                        },
                        "rows": {
                            "type": "array",
                            "description": "Row data. Each row is a list of cell values in the same order as headers.",
                            "items": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                    "required": ["name", "headers", "rows"],
                },
            },
        },
        "required": ["filename", "sheets", "summary"],
    },
}


def _coerce_cell(value):
    """Модель теперь всегда присылает ячейки строками (см. комментарий у
    SPREADSHEET_TOOL — тип-список ["string","number"] несовместим со схемой
    Gemini). Если строка на самом деле число — приводим к int/float, чтобы
    в Excel это была настоящая цифра, а не текст (иначе SUM/сортировка по
    колонке не будут работать у пользователя)."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    try:
        if "." in stripped or "e" in stripped.lower():
            return float(stripped)
        return int(stripped)
    except ValueError:
        return value


def build_spreadsheet(sheets: list) -> bytes:
    """Собирает .xlsx и возвращает байты (загрузка в R2 — в app/tools)."""
    workbook = Workbook()
    workbook.remove(workbook.active)  # убираем дефолтный пустой лист

    for sheet_data in sheets:
        sheet_name = (sheet_data.get("name") or "Sheet")[:31]  # лимит Excel на длину имени листа
        sheet = workbook.create_sheet(title=sheet_name)

        headers = sheet_data.get("headers", [])
        if headers:
            sheet.append(headers)

        for row in sheet_data.get("rows", []):
            sheet.append([_coerce_cell(cell) for cell in row])

    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()
