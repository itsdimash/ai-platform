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
import copy
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.utils.deck.images import generate_deck_images
from app.utils.docx_builder import DOCUMENT_TOOL, build_document
from app.utils.image_builder import IMAGE_TOOL, generate_image_bytes
from app.utils.pdf_builder import PDF_TOOL, build_pdf
from app.utils.pptx_builder import PRESENTATION_TOOL, prepare_deck, render
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

_MAX_LINE = 300


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
    # Файлы ассистента из истории чата (от старых к новым) — нужны, чтобы понять, какую
    # версию файла правит пользователь (поле `revises` в вызове инструмента).
    history_attachments: list[dict] = field(default_factory=list)
    # Файл, который пользователь явно выбрал для правки (кнопка «Изменить»): с ним не нужно
    # угадывать версию по имени из `revises`, и он может быть старше окна истории.
    edit_target: dict | None = None


@dataclass
class ToolResult:
    attachment: dict  # запись вложения в формате БД (без url)
    summary: str  # детерминированное резюме для поля text ответа
    image_model: str | None = None  # реальный id модели картинок (для image_model_used)


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def clean_line(value: object, limit: int = _MAX_LINE) -> str:
    """Одна короткая строка без переводов строк/лишних пробелов (summary и caption
    приходят от модели и попадают прямо в текст ответа)."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].rstrip()


def file_reply(
    emoji: str, kind: str, title: str, details: str, summary: object = "", *, edited: bool = False
) -> str:
    """«📊 Готово! Презентация «X» — 12 слайдов. <summary>»; для правки — «Обновлено!»."""
    lead = "Обновлено!" if edited else "Готово!"
    text = f"{emoji} {lead} {kind} «{title}» — {details}."
    extra = clean_line(summary)
    return f"{text} {extra}" if extra else text


def image_reply(caption: object = "", *, edited: bool = False) -> str:
    """«🎨 Готово: <caption>»; для правки — «Обновлено: <caption>»."""
    text = clean_line(caption)
    lead = "Обновлено" if edited else "Готово"
    return f"🎨 {lead}: {text}" if text else f"🎨 {lead}: изображение создано."


def _is_mime(mime: str):
    return lambda att: att.get("mime") == mime


_is_deck = _is_mime(MIME_PPTX)
_is_docx = _is_mime(MIME_DOCX)
_is_xlsx = _is_mime(MIME_XLSX)
_is_pdf = _is_mime(MIME_PDF)


def _is_image(att: dict) -> bool:
    return att.get("type") == "image"


def resolve_previous(ctx: ToolContext, args: dict, matches) -> dict | None:
    """Убирает `revises` из аргументов и возвращает правимый файл: выбранный пользователем
    (ctx.edit_target) или найденный по имени из `revises`."""
    revises = args.pop("revises", None)
    if ctx.edit_target and matches(ctx.edit_target):
        return ctx.edit_target
    return previous_version(ctx, revises, matches)


def previous_version(ctx: ToolContext, revises: object, matches) -> dict | None:
    """Файл из истории, который правит пользователь: по точному имени из `revises`,
    а если модель имя не угадала — самый свежий файл того же вида."""
    wanted = clean_line(revises).casefold()
    if not wanted:
        return None
    same_kind = [a for a in ctx.history_attachments if matches(a)]
    for att in reversed(same_kind):
        if str(att.get("name", "")).casefold() == wanted:
            return att
    return same_kind[-1] if same_kind else None


def revision_of(prev: dict | None) -> int:
    """Номер версии новой правки: исходный файл = v1, первая правка = v2."""
    return int(prev.get("revision") or 1) + 1 if prev else 1


_VERSION_SUFFIX = re.compile(r"\s*\(v\d+\)\s*$")


def base_title(title: str) -> str:
    """Название без суффикса версии, который модель могла скопировать из имени файла."""
    return _VERSION_SUFFIX.sub("", title) or title


def with_version(title: str, revision: int) -> str:
    """«План» + v2 -> «План (v2)»; суффикс версии срезается, чтобы не получилось
    «План (v2) (v3)»."""
    title = base_title(title)
    return f"{title} (v{revision})" if revision > 1 else title


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
        # Нормализация и раскладка слайдов -> картинки (параллельно, с общим таймаутом) ->
        # сборка файла в потоке. Сбой картинки не ломает колоду.
        revision = revision_of(resolve_previous(ctx, args, _is_deck))
        spec = copy.deepcopy(args)  # до normalize: это ровно то, что прислала модель
        deck, slides = prepare_deck(args)
        images, image_model = await generate_deck_images(deck, slides)
        rendered = await asyncio.to_thread(render, deck, slides, images)
        title = with_version(clean_line(deck.title) or "Презентация", revision)
        record = await _store(ctx, f"{title}.pptx", rendered.data, MIME_PPTX)
        # Исходные данные колоды — чтобы следующая правка шла от них, а не с нуля.
        record.update(spec=spec, revision=revision)
        count = _plural(rendered.slide_count, "слайд", "слайда", "слайдов")
        reply = file_reply("📊", "Презентация", title, count, deck.summary, edited=revision > 1)
        return ToolResult(record, reply, image_model=image_model)

    if name == "generate_document":
        revision = revision_of(resolve_previous(ctx, args, _is_docx))
        spec = copy.deepcopy(args)
        title = base_title(clean_line(args.get("title"))) or "Документ"
        sections = args.get("sections") or []
        data = await asyncio.to_thread(
            build_document, title, sections, clean_line(args.get("subtitle"))
        )
        shown = with_version(title, revision)
        record = await _store(ctx, f"{shown}.docx", data, MIME_DOCX)
        record.update(spec=spec, revision=revision)
        count = _plural(len(sections), "раздел", "раздела", "разделов")
        reply = file_reply("📄", "Документ", shown, count, args.get("summary"), edited=revision > 1)
        return ToolResult(record, reply)

    if name == "generate_spreadsheet":
        revision = revision_of(resolve_previous(ctx, args, _is_xlsx))
        spec = copy.deepcopy(args)
        filename = (
            base_title(clean_line(args.get("filename")).replace("_", " ").strip()) or "Таблица"
        )
        sheets = args.get("sheets") or []
        data = await asyncio.to_thread(build_spreadsheet, sheets)
        shown = with_version(filename, revision)
        record = await _store(ctx, f"{shown}.xlsx", data, MIME_XLSX)
        record.update(spec=spec, revision=revision)
        rows = sum(len(s.get("rows") or []) for s in sheets)
        info = f"{_plural(len(sheets), 'лист', 'листа', 'листов')}, {_plural(rows, 'строка', 'строки', 'строк')}"
        reply = file_reply("📈", "Таблица", shown, info, args.get("summary"), edited=revision > 1)
        return ToolResult(record, reply)

    if name == "generate_pdf":
        revision = revision_of(resolve_previous(ctx, args, _is_pdf))
        spec = copy.deepcopy(args)
        title = base_title(clean_line(args.get("title"))) or "Документ"
        sections = args.get("sections") or []
        data = await asyncio.to_thread(build_pdf, title, sections, clean_line(args.get("subtitle")))
        shown = with_version(title, revision)
        record = await _store(ctx, f"{shown}.pdf", data, MIME_PDF)
        record.update(spec=spec, revision=revision)
        count = _plural(len(sections), "раздел", "раздела", "разделов")
        reply = file_reply("📕", "PDF", shown, count, args.get("summary"), edited=revision > 1)
        return ToolResult(record, reply)

    if name == "generate_image":
        revision = revision_of(resolve_previous(ctx, args, _is_image))
        image_prompt = args.get("prompt") or prompt
        data, image_model = await generate_image_bytes(prompt=image_prompt, size=args.get("size"))
        caption = clean_line(args.get("caption"))
        base = with_version(caption[:60].rstrip(" .,;:!?…") or "image", revision)
        record = await _store(ctx, f"{base}.png", data, MIME_PNG)
        record.update(
            spec={"prompt": image_prompt, "size": args.get("size") or "", "caption": caption},
            revision=revision,
        )
        reply = image_reply(caption, edited=revision > 1)
        return ToolResult(record, reply, image_model=image_model)

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
    написанный до выполнения инструмента, звучит как обещание), файлы попадают в
    result.attachments, а реальный id модели картинок — в result.image_model.
    Если инструментов не было — result не меняется."""
    tool_results = await run_tool_calls(result.tool_calls, ctx=ctx, prompt=prompt)
    if tool_results:
        result.text = "\n".join(r.summary for r in tool_results)
        result.attachments = [r.attachment for r in tool_results]
        result.image_model = next((r.image_model for r in tool_results if r.image_model), None)
    return result
