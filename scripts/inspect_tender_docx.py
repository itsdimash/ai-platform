"""Ручная проверка тендерного .docx БЕЗ вызовов модели: где найдено описание характеристик.

    uv run python scripts/inspect_tender_docx.py путь/к/файлу.docx
    uv run python -m scripts.inspect_tender_docx путь/к/файлу.docx

Выводит: в какой таблице и ячейке найдено описание, раскладку (A — заголовок в шапке колонки,
B — заголовок слева), число символов, число маркеров количества («- N шт.»), на сколько частей
текст будет разбит перед этапом 1 и первые 300 символов. Файл только читается.
Код возврата: 0 — ячейка найдена, 1 — не найдена или файл не прочитан, 2 — неверные аргументы.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from app.templates.pipelines.tender_products import (
    DEFAULT_CHUNK_CHARS,
    count_markers,
    split_text,
)
from app.utils.docx_cells import find_description_cells

TEMPLATE_YAML = (
    Path(__file__).resolve().parents[1] / "prompts" / "tender_products" / "template.yaml"
)
LAYOUTS = {
    "A": "A — заголовок в шапке колонки, содержимое ниже",
    "B": "B — заголовок слева, содержимое справа",
}


def chunk_limit() -> int:
    try:
        settings = yaml.safe_load(TEMPLATE_YAML.read_text(encoding="utf-8")).get("settings", {})
        return int(settings.get("split_chunk_chars", DEFAULT_CHUNK_CHARS))
    except (OSError, ValueError, AttributeError):
        return DEFAULT_CHUNK_CHARS


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Использование: inspect_tender_docx.py <файл.docx>", file=sys.stderr)
        return 2
    path = Path(args[0])
    try:
        data = path.read_bytes()
        matches = find_description_cells(data)
    except Exception as exc:  # noqa: BLE001 — диагностический скрипт: показываем причину
        print(f"Не удалось прочитать {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"Файл: {path.name} ({len(data)} байт)")
    if not matches:
        print(
            "Ячейка с описанием характеристик НЕ найдена (run завершился бы кодом tender_cell_not_found)."
        )
        return 1

    limit = chunk_limit()
    print(
        f"Найдено ячеек: {len(matches)}"
        + (" (тексты будут объединены, будет warning)" if len(matches) > 1 else "")
    )
    for number, match in enumerate(matches, 1):
        parts = split_text(match.text, limit) if len(match.text) > limit else [match.text]
        print(f"\n--- Ячейка {number} ---")
        print(f"Где: {match.path}, заголовок в строке {match.row}, колонке {match.col}")
        print(f"Раскладка: {LAYOUTS[match.layout]}")
        print(f"Символов: {len(match.text)}")
        print(f"Маркеров количества («- N шт.» и т.п.): {count_markers(match.text)}")
        print(f"Частей перед этапом 1: {len(parts)} (порог {limit} симв.)")
        if len(parts) > 1:
            print("  размеры частей: " + ", ".join(str(len(p)) for p in parts))
        preview = match.text[:300].replace("\n", " ⏎ ")
        print(f"Первые 300 символов:\n  {preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
