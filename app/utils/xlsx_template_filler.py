"""Заполнение присланного Excel-шаблона закупочной спецификации (без LLM).

Результат — та же книга с добавленными строками, а не новый файл: стили строк копируются с
ближайшей существующей строки данных, ширины колонок и шапка не трогаются, формулы
сохраняются (книга открывается без read_only и data_only).

Колонки определяются по заголовкам, а не по номерам: №, Раздел, Наименование, Спецификация,
Ед., Кол-во, Фото/ссылка товаров (нормализация регистра, пробелов и точек, допускаются
небольшие отличия формулировок). Не найдена хотя бы одна — XlsxTemplateError.

Запись начинается с первой пустой строки после шапки и уже существующих строк/разделов;
нумерация продолжает последнее число в колонке «№». Если под этой строкой есть данные, которые
запись затронула бы, файл не портится — XlsxTemplateError с номерами строк.
"""

import io
import re
import zipfile
from copy import copy
from dataclasses import dataclass, field

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.styles import Alignment, Font
from openpyxl.worksheet.worksheet import Worksheet

HEADER_SCAN_ROWS = 60
MIN_HEADER_KEYS = 3  # меньше распознанных заголовков в строке — это не шапка таблицы
COLUMN_LABELS = {
    "num": "№",
    "section": "Раздел",
    "name": "Наименование",
    "spec": "Спецификация",
    "unit": "Ед.",
    "qty": "Кол-во",
    "link": "Фото/ссылка товаров",
}
LINK_TEXT = "Ссылка"
HYPERLINK_COLOR = "0563C1"

_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_NON_WORD = re.compile(r"[\s.,:;()\-_/\\]+")

# Объекты, которые openpyxl может потерять при загрузке/сохранении книги.
LOSS_PARTS = (
    ("xl/drawings/", "рисунки или фигуры"),
    ("xl/charts/", "диаграммы"),
    ("xl/media/", "изображения"),
    ("xl/vbaProject.bin", "макросы (VBA)"),
    ("xl/pivotTables/", "сводные таблицы"),
    ("xl/slicers/", "срезы"),
)


class XlsxTemplateError(Exception):
    """Шаблон нельзя заполнить; message показывается пользователю."""


@dataclass
class TemplateLayout:
    sheet: str | None = None
    header_row: int | None = None  # строка, в которой найдены заголовки
    header_bottom: int | None = None  # последняя строка шапки (с учётом объединений)
    columns: dict[str, int] = field(default_factory=dict)  # ключ -> номер колонки (с 1)
    missing: list[str] = field(default_factory=list)  # подписи ненайденных колонок
    start_row: int | None = None
    next_number: int = 1
    style_row: int | None = None
    span: tuple[int, int] | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class SpecRow:
    name: str
    specification: str
    unit: str
    quantity: int | float
    section: str | None = None
    link_url: str | None = None  # проверенный URL -> гиперссылка
    link_note: str | None = None  # текст вместо ссылки («[требуется проверка]» и т.п.)


@dataclass
class FillResult:
    data: bytes
    layout: TemplateLayout
    added: int
    first_row: int
    last_row: int


# --- заголовки -------------------------------------------------------------------------------


def normalize_header(value) -> str:
    text = str(value).casefold().replace("ё", "е").replace("№", "no")
    return _NON_WORD.sub("", text)


def header_key(value) -> str | None:
    """Ключ колонки по тексту заголовка или None."""
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        return None
    n = normalize_header(value)
    if n in ("n", "пп") or n.startswith(("no", "номер")):
        return "num"
    if "фото" in n or "ссылк" in n:
        return "link"
    if n.startswith("кол") or "количеств" in n:
        return "qty"
    if n == "ед" or n.startswith("едиз") or "единиц" in n:
        return "unit"
    if "раздел" in n:
        return "section"
    if "спецификац" in n or "характеристик" in n or "описание" in n:
        return "spec"
    if "наименован" in n or n in ("товар", "название"):
        return "name"
    return None


def _merged_bottom(ws: Worksheet, row: int, col: int) -> int:
    for mr in ws.merged_cells.ranges:
        if mr.min_row <= row <= mr.max_row and mr.min_col <= col <= mr.max_col:
            return mr.max_row
    return row


def _merged_right(ws: Worksheet, row: int, col: int) -> int:
    for mr in ws.merged_cells.ranges:
        if mr.min_row <= row <= mr.max_row and mr.min_col <= col <= mr.max_col:
            return mr.max_col
    return col


def _find_header(wb) -> tuple[Worksheet | None, int, dict[str, int]]:
    """Лучшая (по числу распознанных колонок) строка заголовков среди видимых листов."""
    best: tuple[Worksheet | None, int, dict[str, int]] = (None, 0, {})
    for ws in wb.worksheets:
        if ws.sheet_state != "visible":
            continue
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, HEADER_SCAN_ROWS)):
            columns: dict[str, int] = {}
            for cell in row:
                if isinstance(cell, MergedCell):
                    continue
                key = header_key(cell.value)
                if key and key not in columns:
                    columns[key] = cell.column
            if len(columns) >= MIN_HEADER_KEYS and len(columns) > len(best[2]):
                best = (ws, row[0].row, columns)
    return best


# --- анализ шаблона -------------------------------------------------------------------------


def _has_content(ws: Worksheet, row: int, min_col: int, max_col: int) -> bool:
    for col in range(min_col, max_col + 1):
        value = ws.cell(row=row, column=col).value
        if value is not None and str(value).strip() != "":
            return True
    return False


def _is_section_row(ws: Worksheet, row: int, min_col: int, max_col: int) -> bool:
    width = max_col - min_col + 1
    return any(
        mr.min_row == mr.max_row == row and (mr.max_col - mr.min_col + 1) >= max(2, width * 0.6)
        for mr in ws.merged_cells.ranges
    )


def _number_of(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and float(value).is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().rstrip(".").isdigit():
        return int(value.strip().rstrip("."))
    return None


def loss_warnings(data: bytes) -> list[str]:
    """Предупреждения об объектах, которые openpyxl не сохранит (картинки, диаграммы, макросы)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile:
        return []
    warnings = []
    for prefix, label in LOSS_PARTS:
        if any(n == prefix or n.startswith(prefix) for n in names):
            warnings.append(
                f"В шаблоне есть объекты ({label}), которые могут быть потеряны при заполнении: "
                "проверьте итоговый файл."
            )
    return warnings


def analyze_workbook(wb, data: bytes) -> TemplateLayout:
    """Раскладка шаблона: лист, шапка, колонки, начало записи. Ничего не меняет и не бросает:
    ненайденные колонки перечислены в layout.missing."""
    layout = TemplateLayout(warnings=loss_warnings(data))
    ws, header_row, columns = _find_header(wb)
    if ws is None:
        layout.missing = list(COLUMN_LABELS.values())
        return layout

    layout.sheet, layout.header_row, layout.columns = ws.title, header_row, columns
    layout.missing = [label for key, label in COLUMN_LABELS.items() if key not in columns]
    layout.header_bottom = max(_merged_bottom(ws, header_row, col) for col in columns.values())
    min_col = min(columns.values())
    max_col = max(_merged_right(ws, header_row, col) for col in columns.values())
    layout.span = (min_col, max_col)

    # Первая пустая строка после шапки и существующих строк/разделов.
    row = layout.header_bottom + 1
    last_data: int | None = None
    last_number = 0
    num_col = columns.get("num")
    while row <= ws.max_row and _has_content(ws, row, min_col, max_col):
        if not _is_section_row(ws, row, min_col, max_col):
            last_data = row
        if num_col:
            number = _number_of(ws.cell(row=row, column=num_col).value)
            if number is not None:
                last_number = number
        row += 1
    layout.start_row = row
    layout.next_number = last_number + 1
    if last_data is not None:
        layout.style_row = last_data
    elif layout.header_bottom + 1 <= ws.max_row:
        layout.style_row = layout.header_bottom + 1
    return layout


def inspect_template(data: bytes) -> TemplateLayout:
    """Анализ шаблона по байтам для диагностики (ошибок не бросает)."""
    try:
        wb = load_workbook(io.BytesIO(data))
    except Exception as exc:
        raise XlsxTemplateError("Не удалось прочитать Excel-файл шаблона.") from exc
    return analyze_workbook(wb, data)


def require_complete(layout: TemplateLayout) -> None:
    if layout.header_row is None:
        raise XlsxTemplateError(
            "В шаблоне не найдена строка заголовков таблицы. Ожидаются колонки: "
            + ", ".join(COLUMN_LABELS.values())
            + "."
        )
    if layout.missing:
        raise XlsxTemplateError(
            f"В шаблоне (лист «{layout.sheet}», строка {layout.header_row}) не найдены колонки: "
            + ", ".join(layout.missing)
            + "."
        )


# --- запись ------------------------------------------------------------------------------------


def _anchor(ws: Worksheet, row: int, col: int):
    """Ячейка для записи: если (row, col) внутри объединения — его левая верхняя ячейка."""
    cell = ws.cell(row=row, column=col)
    if not isinstance(cell, MergedCell):
        return cell
    for mr in ws.merged_cells.ranges:
        if mr.min_row <= row <= mr.max_row and mr.min_col <= col <= mr.max_col:
            return ws.cell(row=mr.min_row, column=mr.min_col)
    return cell


def _in_merge(ws: Worksheet, row: int, col: int) -> bool:
    return isinstance(ws.cell(row=row, column=col), MergedCell) or any(
        mr.min_row <= row <= mr.max_row and mr.min_col <= col <= mr.max_col
        for mr in ws.merged_cells.ranges
    )


def _clean(value):
    return _ILLEGAL.sub("", value) if isinstance(value, str) else value


def _set(cell, value) -> None:
    cell.value = _clean(value)
    if isinstance(cell.value, str) and cell.value.startswith("="):
        cell.data_type = "s"  # текст, а не формула


def _copy_row_style(ws: Worksheet, src: int, dst: int, span: tuple[int, int]) -> None:
    min_col, max_col = span
    for col in range(min_col, max_col + 1):
        source = ws.cell(row=src, column=col)
        target = ws.cell(row=dst, column=col)
        if source.has_style and not isinstance(target, MergedCell):
            target._style = copy(source._style)
    height = ws.row_dimensions[src].height
    if height is not None:
        ws.row_dimensions[dst].height = height
    for mr in list(ws.merged_cells.ranges):  # объединения строки-образца повторяем
        inside = mr.min_row == mr.max_row == src and mr.min_col >= min_col and mr.max_col <= max_col
        if inside and not _in_merge(ws, dst, mr.min_col):
            ws.merge_cells(
                start_row=dst, end_row=dst, start_column=mr.min_col, end_column=mr.max_col
            )


def fill_template(
    data: bytes, rows: list[SpecRow], default_section: str | None = None
) -> FillResult:
    """Дописывает rows в шаблон. Возвращает байты новой книги и раскладку."""
    try:
        wb = load_workbook(io.BytesIO(data))
    except Exception as exc:
        raise XlsxTemplateError("Не удалось прочитать Excel-файл шаблона.") from exc
    layout = analyze_workbook(wb, data)
    require_complete(layout)
    ws = wb[layout.sheet]
    assert layout.span and layout.start_row  # гарантируется require_complete
    start, count = layout.start_row, len(rows)

    if count:
        busy = [
            r
            for r in range(start, start + count)
            if r <= ws.max_row and _has_content(ws, r, *layout.span)
        ]
        if busy:
            raise XlsxTemplateError(
                f"Под таблицей (строки {busy[0]}–{busy[-1]}) есть данные, которые запись "
                f"{count} строк перезаписала бы. Освободите место или перенесите нижний блок."
            )

    cols = layout.columns
    for i, row in enumerate(rows):
        r = start + i
        if layout.style_row is not None:
            _copy_row_style(ws, layout.style_row, r, layout.span)
        section = row.section or default_section
        _set(_anchor(ws, r, cols["num"]), layout.next_number + i)
        _set(_anchor(ws, r, cols["section"]), section or None)
        _set(_anchor(ws, r, cols["name"]), row.name)
        spec_cell = _anchor(ws, r, cols["spec"])
        _set(spec_cell, row.specification)
        alignment = copy(spec_cell.alignment)
        spec_cell.alignment = Alignment(
            horizontal=alignment.horizontal,
            vertical=alignment.vertical or "top",
            wrap_text=True,
        )
        _set(_anchor(ws, r, cols["unit"]), row.unit)
        _set(_anchor(ws, r, cols["qty"]), row.quantity)
        link_cell = _anchor(ws, r, cols["link"])
        if row.link_url:
            link_cell.value = LINK_TEXT
            link_cell.hyperlink = row.link_url
            font = copy(link_cell.font)
            link_cell.font = Font(
                name=font.name,
                size=font.size,
                bold=font.bold,
                italic=font.italic,
                color=HYPERLINK_COLOR,
                underline="single",
            )
        else:
            _set(link_cell, row.link_note)

    buf = io.BytesIO()
    wb.save(buf)
    return FillResult(
        data=buf.getvalue(),
        layout=layout,
        added=count,
        first_row=start,
        last_row=start + count - 1,
    )
