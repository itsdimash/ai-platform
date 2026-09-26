"""Генерация Excel-таблицы (.xlsx) по вызову инструмента generate_spreadsheet.

Зеркалит app/utils/pptx_builder.py: модель отдаёт структурированные данные
(листы -> заголовки колонок + строки), мы собираем реальный .xlsx через
openpyxl и заливаем в R2, возвращая публичную ссылку.
"""
import io

from openpyxl import Workbook

from app.utils.r2 import upload_file_to_r2

SPREADSHEET_TOOL = {
    "name": "generate_spreadsheet",
    "description": (
        "Generates an Excel spreadsheet (.xlsx) from tabular data and "
        "returns a download link. Use this when the user asks for an "
        "export, a data file, or a table they explicitly want to download "
        "— NOT for small tables that are more useful shown directly in "
        "the chat reply."
    ),
    "parameters": {
        "type": "object",
        "properties": {
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
        "required": ["filename", "sheets"],
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


def create_spreadsheet_file(filename: str, sheets: list) -> str:
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
    file_bytes = stream.getvalue()

    safe_name = "".join(c if c.isalnum() else "_" for c in filename)[:20]
    out_filename = f"{safe_name}.xlsx"

    return upload_file_to_r2(
        file_bytes=file_bytes,
        original_filename=out_filename,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        folder="spreadsheets",
    )
