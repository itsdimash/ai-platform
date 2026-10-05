"""Единый исполнитель инструментов-генераторов файлов.

Адаптеры только достают из ответа провайдера вызовы инструментов
(GenerationResult.tool_calls = [{"name", "args"}]) — исполняет их apply_tool_calls
после генерации: собирает файл, кладёт в R2 под ключ
ai/{user_id}/{session_id}/{uuid4hex}_{name} и возвращает запись вложения.

Любой сбой билдера / загрузки в R2 / генерации изображения превращается в
ToolExecutionError — роутеры чата отвечают на него 5xx и НЕ сохраняют сбой в
историю как ответ ассистента.
"""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.utils.docx_builder import DOCUMENT_TOOL, build_document
from app.utils.image_builder import IMAGE_TOOL, generate_image_bytes
from app.utils.pdf_builder import PDF_TOOL, build_pdf
from app.utils.pptx_builder import PRESENTATION_TOOL, build_presentation
from app.utils.r2 import put_bytes_async
from app.utils.storage import build_key, display_name, make_record
from app.utils.xlsx_builder import SPREADSHEET_TOOL, build_spreadsheet

if TYPE_CHECKING:  # base.py импортирует ALL_TOOLS отсюда — рантайм-импорт дал бы цикл
    from app.adapters.base import GenerationResult

ALL_TOOLS = [PRESENTATION_TOOL, IMAGE_TOOL, DOCUMENT_TOOL, SPREADSHEET_TOOL, PDF_TOOL]

MIME_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_PDF = "application/pdf"
MIME_PNG = "image/png"


class ToolExecutionError(Exception):
    """Инструмент вызван моделью корректно, но выполнить его не удалось."""

    def __init__(self, tool_name: str, cause: Exception):
        super().__init__(f"{tool_name}: {type(cause).__name__}: {cause}")
        self.tool_name = tool_name
        self.cause = cause


@dataclass
class ToolContext:
    user_id: int
    session_id: int


@dataclass
class ToolResult:
    attachment: dict  # запись вложения в формате БД (без url)
    summary: str  # детерминированное резюме для поля text ответа


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


async def _store(ctx: ToolContext, filename: str, data: bytes, mime: str) -> dict:
    name = display_name(filename)
    key = build_key(ctx.user_id, ctx.session_id, name)
    await put_bytes_async(key, data, mime, name=name)
    return make_record(name=name, key=key, mime=mime, size=len(data))


async def _run_one(
    name: str, args: dict[str, Any], ctx: ToolContext, prompt: str
) -> ToolResult | None:
    """Выполняет один tool-вызов; None, если имя инструмента не наше
    (например, серверные tools провайдера)."""
    if name == "generate_presentation":
        title = args.get("title") or "Презентация"
        slides = args.get("slides") or []
        data = await asyncio.to_thread(build_presentation, title, args.get("subtitle", ""), slides)
        record = await _store(ctx, f"{title}.pptx", data, MIME_PPTX)
        count = _plural(len(slides) + 1, "слайд", "слайда", "слайдов")
        return ToolResult(record, f"📊 Готово! Презентация «{title}» — {count}.")

    if name == "generate_document":
        title = args.get("title") or "Документ"
        sections = args.get("sections") or []
        data = await asyncio.to_thread(build_document, title, sections)
        record = await _store(ctx, f"{title}.docx", data, MIME_DOCX)
        count = _plural(len(sections), "раздел", "раздела", "разделов")
        return ToolResult(record, f"📄 Готово! Документ «{title}» — {count}.")

    if name == "generate_spreadsheet":
        filename = args.get("filename") or "Таблица"
        sheets = args.get("sheets") or []
        data = await asyncio.to_thread(build_spreadsheet, sheets)
        record = await _store(ctx, f"{filename}.xlsx", data, MIME_XLSX)
        rows = sum(len(s.get("rows") or []) for s in sheets)
        info = f"{_plural(len(sheets), 'лист', 'листа', 'листов')}, {_plural(rows, 'строка', 'строки', 'строк')}"
        return ToolResult(record, f"📈 Готово! Таблица «{filename}» — {info}.")

    if name == "generate_pdf":
        title = args.get("title") or "Документ"
        sections = args.get("sections") or []
        data = await asyncio.to_thread(build_pdf, title, sections)
        record = await _store(ctx, f"{title}.pdf", data, MIME_PDF)
        count = _plural(len(sections), "раздел", "раздела", "разделов")
        return ToolResult(record, f"📕 Готово! PDF «{title}» — {count}.")

    if name == "generate_image":
        data = await generate_image_bytes(
            prompt=args.get("prompt") or prompt, size=args.get("size", "1024x1024")
        )
        record = await _store(ctx, "image.png", data, MIME_PNG)
        return ToolResult(record, "🎨 Готово! Изображение создано.")

    return None


async def run_tool_calls(
    calls: list[dict[str, Any]], *, ctx: ToolContext, prompt: str
) -> list[ToolResult]:
    """Выполняет вызовы по порядку. При сбое любого — ToolExecutionError."""
    results: list[ToolResult] = []
    for call in calls:
        name = call.get("name", "")
        try:
            result = await _run_one(name, dict(call.get("args") or {}), ctx, prompt)
        except Exception as exc:
            raise ToolExecutionError(name, exc) from exc
        if result is not None:
            results.append(result)
    return results


async def apply_tool_calls(
    result: "GenerationResult", *, ctx: ToolContext, prompt: str
) -> "GenerationResult":
    """Исполняет tool_calls ответа модели. Если хотя бы один наш инструмент
    отработал — text становится детерминированным резюме (текст модели,
    написанный до выполнения инструмента, звучит как обещание), а файлы
    попадают в result.attachments. Если инструментов не было — result не меняется."""
    tool_results = await run_tool_calls(result.tool_calls, ctx=ctx, prompt=prompt)
    if tool_results:
        result.text = "\n".join(r.summary for r in tool_results)
        result.attachments = [r.attachment for r in tool_results]
    return result
