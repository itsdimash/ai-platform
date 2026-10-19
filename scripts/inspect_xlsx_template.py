"""Ручная проверка Excel-шаблона спецификации БЕЗ вызовов модели.

    uv run python scripts/inspect_xlsx_template.py путь/к/шаблону.xlsx
    uv run python -m scripts.inspect_xlsx_template путь/к/шаблону.xlsx

Выводит: лист и строку заголовков, найденные и ненайденные колонки, строку, с которой начнётся
запись, номер, с которого продолжится нумерация, и предупреждения о возможных потерях
(картинки, диаграммы, макросы). Файл только читается, ничего не записывается.
Код возврата: 0 — шаблон пригоден, 1 — не хватает колонок или файл не прочитан, 2 — аргументы.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl.utils import get_column_letter

from app.utils.xlsx_template_filler import COLUMN_LABELS, inspect_template


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Использование: inspect_xlsx_template.py <шаблон.xlsx>", file=sys.stderr)
        return 2
    path = Path(args[0])
    try:
        data = path.read_bytes()
        layout = inspect_template(data)
    except Exception as exc:  # noqa: BLE001
        print(f"Не удалось прочитать {path}: {exc}", file=sys.stderr)
        return 1

    print(f"Файл: {path.name} ({len(data)} байт)")
    if layout.header_row is None:
        print("Строка заголовков НЕ найдена ни на одном видимом листе.")
        print("Ожидаются колонки: " + ", ".join(COLUMN_LABELS.values()))
        return 1

    print(f"Лист: «{layout.sheet}»")
    bottom = (
        f" (шапка занимает строки {layout.header_row}–{layout.header_bottom})"
        if layout.header_bottom != layout.header_row
        else ""
    )
    print(f"Строка заголовков: {layout.header_row}{bottom}")
    print("Найденные колонки:")
    for key, label in COLUMN_LABELS.items():
        if key in layout.columns:
            print(f"  ✓ {label}: колонка {get_column_letter(layout.columns[key])}")
    if layout.missing:
        print(
            "НЕ найдены колонки: "
            + ", ".join(layout.missing)
            + "  (run завершился бы кодом xlsx_template_invalid)"
        )
    print(f"Запись начнётся со строки: {layout.start_row}")
    print(f"Нумерация продолжится с номера: {layout.next_number}")
    style = layout.style_row
    print(
        f"Стиль копируется со строки: {style if style else 'нет строки-образца (будет оформление по умолчанию)'}"
    )
    if layout.warnings:
        print("Предупреждения:")
        for warning in layout.warnings:
            print(f"  - {warning}")
    else:
        print("Предупреждений о потерях нет.")
    return 1 if layout.missing else 0


if __name__ == "__main__":
    sys.exit(main())
