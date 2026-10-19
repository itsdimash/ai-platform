"""Excel-файл результата шаблона «Тендерная ТЗ → таблица товаров».

Лист «Позиции» — итоговая таблица; лист «Детали» — по каждой позиции кандидаты, статус
соответствия, подтверждённые и неподтверждённые требования, источники."""

import io
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")
_HEADER_FONT = Font(bold=True, color="FFFFFF", name="Calibri", size=11)
_THIN = Side(style="thin", color="BFBFBF")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_WRAP = Alignment(wrap_text=True, vertical="top")

POSITIONS_COLUMNS = ("№", "Товар", "Бренд / Модель (кандидат)", "Кол-во", "Ед.изм")
POSITIONS_WIDTHS = (6, 48, 52, 12, 10)
DETAILS_COLUMNS = (
    "№",
    "Товар",
    "Бренд",
    "Модель",
    "Соответствие",
    "Подтверждённые требования",
    "Не подтверждено / не выполняется",
    "Источник данных",
    "Ссылки из поиска",
)
DETAILS_WIDTHS = (6, 36, 20, 28, 22, 50, 50, 40, 50)


def safe_text(value):
    """Строки не должны превращаться в формулы или ломать файл управляющими символами."""
    if isinstance(value, str):
        return _ILLEGAL.sub("", value)
    return value


def _write(ws, columns, widths, rows) -> None:
    ws.append(list(columns))
    for row in rows:
        ws.append([safe_text(v) for v in row])
    for idx, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    for cell in ws[1]:
        cell.font, cell.fill, cell.border = _HEADER_FONT, _HEADER_FILL, _BORDER
        cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment, cell.border = _WRAP, _BORDER
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"  # не формула
    ws.freeze_panes = "A2"
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions


def build_tender_workbook(table: list[dict], items: list[dict]) -> bytes:
    wb = Workbook()
    positions = wb.active
    positions.title = "Позиции"
    _write(
        positions,
        POSITIONS_COLUMNS,
        POSITIONS_WIDTHS,
        [[row[c] for c in POSITIONS_COLUMNS] for row in table],
    )

    details = wb.create_sheet("Детали")
    rows: list[list] = []
    for item in items:
        links = "\n".join(s["url"] for s in item.get("sources", []))
        if item.get("failed"):
            rows.append([item["index"], item["name"], "требует проверки", "", "", "", "", "", ""])
            continue
        candidates = item["result"]["candidates"]
        if not candidates:
            note = item["result"]["overall_note"]
            rows.append([item["index"], item["name"], "", "", "", "", "", note, links])
        for cand in candidates:
            rows.append(
                [
                    item["index"],
                    item["name"],
                    cand["brand"],
                    cand["model"],
                    cand["match_status"],
                    "\n".join(f"• {r}" for r in cand["matched_requirements"]),
                    "\n".join(f"• {r}" for r in cand["unclear_or_failed_requirements"]),
                    cand["source_note"],
                    links,
                ]
            )
    _write(details, DETAILS_COLUMNS, DETAILS_WIDTHS, rows)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
