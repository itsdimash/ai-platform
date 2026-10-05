"""Единый исполнитель инструментов-генераторов файлов.

Раньше код вызова pptx/docx/xlsx/image был скопирован в трёх адаптерах (и в
одном из них часть вызовов не была обёрнута в try/except). Теперь адаптеры
лишь достают из ответа провайдера пары (имя tool, аргументы) и передают сюда.

Любой сбой билдера / загрузки в R2 / генерации изображения превращается в
ToolExecutionError — вызывающий код (роутеры чата) отвечает на него 5xx и НЕ
сохраняет сбой в историю как ответ ассистента.
"""

import asyncio
from collections.abc import Iterable
from typing import Any

from app.utils.docx_builder import DOCUMENT_TOOL, create_document_file
from app.utils.image_builder import IMAGE_TOOL, generate_and_save_image
from app.utils.pptx_builder import PRESENTATION_TOOL, create_presentation_file
from app.utils.xlsx_builder import SPREADSHEET_TOOL, create_spreadsheet_file

ALL_TOOLS = [PRESENTATION_TOOL, IMAGE_TOOL, DOCUMENT_TOOL, SPREADSHEET_TOOL]


class ToolExecutionError(Exception):
    """Инструмент вызван моделью корректно, но выполнить его не удалось."""

    def __init__(self, tool_name: str, cause: Exception):
        super().__init__(f"{tool_name}: {type(cause).__name__}: {cause}")
        self.tool_name = tool_name
        self.cause = cause


async def _run_one(name: str, args: dict[str, Any], prompt: str) -> str | None:
    """Выполняет один tool-вызов, возвращает markdown-текст ответа или None,
    если имя инструмента неизвестно (например, серверные tools провайдера)."""
    if name == "generate_presentation":
        file_url = await asyncio.to_thread(
            create_presentation_file,
            title=args.get("title", "Презентация"),
            subtitle=args.get("subtitle", ""),
            slides_data=args.get("slides", []),
        )
        return (
            f"📊 Готово! Я сформировал презентацию «**{args.get('title')}**».\n\n"
            f"[📥 Скачать презентацию (.pptx)]({file_url})"
        )
    if name == "generate_document":
        file_url = await asyncio.to_thread(
            create_document_file,
            title=args.get("title", "Документ"),
            sections=args.get("sections", []),
        )
        return (
            f"📄 Готово! Я сформировал документ «**{args.get('title')}**».\n\n"
            f"[📥 Скачать документ (.docx)]({file_url})"
        )
    if name == "generate_spreadsheet":
        file_url = await asyncio.to_thread(
            create_spreadsheet_file,
            filename=args.get("filename", "Таблица"),
            sheets=args.get("sheets", []),
        )
        return (
            f"📈 Готово! Я сформировал таблицу «**{args.get('filename')}**».\n\n"
            f"[📥 Скачать таблицу (.xlsx)]({file_url})"
        )
    if name == "generate_image":
        img_url = await generate_and_save_image(
            prompt=args.get("prompt", prompt),
            size=args.get("size", "1024x1024"),
        )
        return f"🎨 Вот изображение по вашему запросу:\n\n![Сгенерированное изображение]({img_url})"
    return None


async def run_tool_calls(calls: Iterable[tuple[str, dict[str, Any]]], *, prompt: str) -> str | None:
    """Выполняет все tool-вызовы ответа модели последовательно.

    Возвращает текст, которым нужно заменить ответ модели (результаты всех
    инструментов через пустую строку), либо None, если ни один из вызовов не
    относится к нашим инструментам. При сбое любого — ToolExecutionError.
    """
    outputs: list[str] = []
    for name, args in calls:
        try:
            text = await _run_one(name, dict(args or {}), prompt)
        except Exception as exc:
            raise ToolExecutionError(name, exc) from exc
        if text is not None:
            outputs.append(text)
    return "\n\n".join(outputs) if outputs else None
