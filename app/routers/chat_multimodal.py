"""Мультимодальный чат: произвольный промпт + изображения / PDF / docx / xlsx.

Отдельный роут POST /v1/chat/multimodal — контракт JSON-роута POST /v1/chat не меняется.
Весь конвейер (классификатор -> роутер -> генерация -> сохранение) общий —
services/chat_service.py; запросы с вложениями тоже проходят через классификатор
(раньше они обходили его с task_type="multimodal"). SQL-путь db_query с вложениями не
используется: такой запрос идёт по правилу general_qa.

Что принимается (utils/uploads.py): png, jpg/jpeg, webp, pdf, docx, xlsx; до 10 файлов,
изображение до 20 МБ, документ до 50 МБ, всё вместе до 100 МБ. Тип определяется по
расширению + magic bytes (Content-Type клиента не используется). Файлы читаются кусками
по 1 МБ с остановкой при превышении лимита.

Как вложения доходят до модели (utils/media.py), решается ПОСЛЕ выбора модели:
- изображения — нативно; если не проходят по лимитам провайдера (Anthropic: 7,5 МБ сырых /
  8000 px по стороне; общий бюджет запроса у всех провайдеров), провайдеру уходит
  уменьшенная копия (Pillow), а в R2 сохраняется оригинал;
- PDF — нативно у Claude/Gemini, если влезает в лимиты провайдера (размер, число
  страниц, бюджет запроса); иначе (и всегда у OpenAI) — текстовый слой, вклеенный в prompt;
- docx/xlsx — текст извлекается на бэкенде и вклеивается в prompt.

Хранение вложений: после УСПЕШНОЙ генерации исходные файлы сохраняются в R2 под
ai/{user_id}/{session_id}/uploads/... и привязываются к user-сообщению
(chat_messages.attachments), чтобы фронт восстановил их из истории. Модель при follow-up
по истории сами вложения не видит — в контекст попадает только пометка об имени файла.

attachment_keys — только привязка ранее загруженных (через /v1/documents/extract) файлов:
владелец и существование проверяются, текст в промпт не извлекается.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.anthropic_adapter import AnthropicAdapter
from ..adapters.base import Attachment, ModelAdapter
from ..adapters.gemini_adapter import GeminiAdapter
from ..adapters.openai_adapter import OpenAIAdapter
from ..auth import CurrentUser, get_current_user
from ..db.session import get_db
from ..limits import (
    MAX_ATTACHMENT_KEYS,
    MAX_EXTRACTED_CHARS_PER_FILE,
    MAX_EXTRACTED_CHARS_TOTAL,
    MAX_FILES_PER_REQUEST,
    MAX_TOTAL_UPLOAD_BYTES,
)
from ..router.route import load_config
from ..services.chat_service import (
    PreparedInput,
    get_or_create_session,
    persist_turn,
    run_turn,
)
from ..services.history import fetch_recent_rows
from ..utils.attachments import (
    describe_kinds,
    kinds_of_records,
    resolve_attachment_keys,
    store_user_upload,
)
from ..utils.media import (
    PROVIDER_ANTHROPIC,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    plan_media,
)
from ..utils.pdf import extract_pdf_pages, pdf_page_count
from ..utils.uploads import ParsedFile, classify_upload, read_upload
from .deps import get_adapters
from .document_extract import _extract_docx, _extract_xlsx
from .schemas import ChatResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# Эвристика «скан / сложная вёрстка»: если текстовый слой даёт меньше
# символов на страницу, чем этот порог, — PDF почти наверняка скан или
# тяжёлая вёрстка, и pdfplumber-текст будет бесполезен.
PDF_SCAN_CHARS_PER_PAGE_THRESHOLD = 100


def _provider_of(adapter: ModelAdapter | None) -> str | None:
    if isinstance(adapter, AnthropicAdapter):
        return PROVIDER_ANTHROPIC
    if isinstance(adapter, GeminiAdapter):
        return PROVIDER_GEMINI
    if isinstance(adapter, OpenAIAdapter):
        return PROVIDER_OPENAI
    return None


def _supports_native_pdf(adapter: ModelAdapter | None) -> bool:
    """Claude и Gemini принимают PDF нативно; OpenAI — нет."""
    return _provider_of(adapter) in (PROVIDER_ANTHROPIC, PROVIDER_GEMINI)


async def _read_and_validate(files: list[UploadFile] | None) -> list[ParsedFile]:
    """Читает файлы кусками, валидирует расширение + magic bytes и лимиты.
    Любое нарушение — HTTP 400."""
    uploads = files or []
    if len(uploads) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Не больше {MAX_FILES_PER_REQUEST} файлов в одном сообщении",
        )

    parsed: list[ParsedFile] = []
    total = 0
    for upload in uploads:
        raw = await read_upload(upload, total_so_far=total, max_total=MAX_TOTAL_UPLOAD_BYTES)
        if not raw:
            continue
        total += len(raw)
        parsed.append(classify_upload(upload.filename, raw))
    return parsed


def _pdf_text(data: bytes) -> tuple[str, int]:
    """Текстовый слой PDF (через "\\n") + число страниц с текстом."""
    pages = extract_pdf_pages(data)
    return "\n".join(pages), len(pages)


def _safe_page_count(data: bytes) -> int:
    try:
        return pdf_page_count(data)
    except Exception:  # noqa: BLE001 — битый PDF не должен ронять весь запрос; решит провайдер
        return 0


def _parse_session_id(session_id: str | None) -> int | None:
    if session_id is None or session_id.strip() in ("", "null", "undefined"):
        return None
    try:
        return int(session_id)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="session_id должен быть целым числом"
        ) from exc


class _TextBudget:
    """Общий потолок на текст, вклеиваемый в промпт из всех документов запроса."""

    def __init__(self) -> None:
        self.remaining = MAX_EXTRACTED_CHARS_TOTAL

    def take(self, text: str) -> tuple[str, bool]:
        limit = min(MAX_EXTRACTED_CHARS_PER_FILE, self.remaining)
        snippet = text[:limit]
        self.remaining -= len(snippet)
        return snippet, len(text) > len(snippet)


async def _prepare_input(
    adapter: ModelAdapter,
    images: list[ParsedFile],
    pdfs: list[ParsedFile],
    office: list[ParsedFile],
) -> PreparedInput:
    """Подготовка вложений под лимиты выбранного провайдера. Pillow / pdfplumber —
    CPU-тяжёлые, при файлах до 50 МБ их нельзя гонять в event loop. Ошибки валидации
    (HTTPException) пробрасываются как есть."""
    provider = _provider_of(adapter) or PROVIDER_OPENAI
    pdf_pages = [await asyncio.to_thread(_safe_page_count, p.data) for p in pdfs]
    plan = await asyncio.to_thread(
        plan_media,
        provider,
        images=images,
        pdfs=pdfs,
        pdf_pages=pdf_pages,
        is_200k_context="haiku" in adapter.model.lower(),
    )

    attachments: list[Attachment] = [
        Attachment(mime_type=img.mime, data=img.data, kind="image") for img in plan.images
    ]
    attachments += [
        Attachment(mime_type="application/pdf", data=pdf.data, kind="pdf_document")
        for pdf in plan.native_pdfs
    ]

    # Текстовый путь: PDF вне лимитов провайдера (и все PDF у OpenAI), docx, xlsx.
    budget = _TextBudget()
    suffix_parts: list[str] = []
    native_provider = _supports_native_pdf(adapter)

    for pdf in plan.text_pdfs:
        try:
            text, text_pages = await asyncio.to_thread(_pdf_text, pdf.data)
        except Exception:  # noqa: BLE001 — битый PDF не должен ронять весь запрос
            text, text_pages = "", 0
        chars_per_page = len(text) / text_pages if text_pages else 0.0
        scan_like = text_pages == 0 or chars_per_page < PDF_SCAN_CHARS_PER_PAGE_THRESHOLD
        snippet, was_truncated = budget.take(text)
        if native_provider:
            note = " (слишком велик для нативной передачи модели — передан текстовый слой)"
        elif scan_like:
            # TODO: чтобы поддержать сканы в GPT, рендерить страницы PDF
            # в PNG и слать их как image-вложения (pdf2image / pymupdf).
            note = (
                " (низкое качество: похоже на скан/сложную вёрстку, "
                "для таких файлов выберите Claude или Gemini)"
            )
        else:
            note = ""
        if native_provider and scan_like:
            note += "; похоже на скан — текст может быть неполным"
        marker = " [обрезано]" if was_truncated else ""
        suffix_parts.append(f"\n\n=== Содержимое PDF «{pdf.filename}»{note}{marker} ===\n{snippet}")

    for doc in office:
        extractor = _extract_docx if doc.kind == "docx" else _extract_xlsx
        try:
            text = await asyncio.to_thread(extractor, doc.data)
        except Exception:  # noqa: BLE001 — битый документ не должен ронять весь запрос
            text = ""
        snippet, was_truncated = budget.take(text)
        marker = " [обрезано]" if was_truncated else ""
        label = "DOCX" if doc.kind == "docx" else "XLSX"
        suffix_parts.append(f"\n\n=== Содержимое {label} «{doc.filename}»{marker} ===\n{snippet}")

    notes: list[str] = []
    if plan.resized_names:
        logger.info("images downscaled for %s: %d", provider, len(plan.resized_names))
        notes.append(f"Изображений уменьшено для провайдера: {len(plan.resized_names)}")
    return PreparedInput(attachments, "".join(suffix_parts), notes)


@router.post("/v1/chat/multimodal", response_model=ChatResponse)
async def chat_multimodal(
    prompt: str = Form(...),
    session_id: str | None = Form(None),
    model: str | None = Form(None),
    files: list[UploadFile] | None = File(None),
    # Ключи файлов, ранее загруженных через /v1/documents/extract (file_key).
    attachment_keys: list[str] | None = Form(None),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    adapters: dict[str, ModelAdapter] = Depends(get_adapters),
) -> ChatResponse:
    started = time.monotonic()

    body_model = model.strip() if model and model.strip() else None
    sid = _parse_session_id(session_id)

    # Как и в /v1/chat (там это 422 от схемы): лишние ключи не отбрасываем молча.
    keys = attachment_keys or []
    if len(keys) > MAX_ATTACHMENT_KEYS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Не больше {MAX_ATTACHMENT_KEYS} ключей вложений в одном сообщении",
        )

    parsed = await _read_and_validate(files)
    images = [f for f in parsed if f.kind == "image"]
    pdfs = [f for f in parsed if f.kind == "pdf"]
    office_docs = [f for f in parsed if f.kind in ("docx", "xlsx")]

    # Только привязка: по ключам проверяется владелец и существование (HEAD), текст
    # документа в промпт НЕ извлекается — клиент вклеивает его сам (см. /extract).
    key_attachments = await resolve_attachment_keys(keys, user.user_id)
    kinds = [f.kind for f in parsed] + kinds_of_records(key_attachments)

    session = await get_or_create_session(db, sid, user.user_id, prompt)
    history_rows = await fetch_recent_rows(db, session.id, load_config()["history"]["max_messages"])

    outcome = await run_turn(
        db=db,
        adapters=adapters,
        user=user,
        session=session,
        session_created=sid is None,
        started=started,
        user_text=prompt,
        model_choice=body_model,
        history_rows=history_rows,
        attachment_kinds=describe_kinds(kinds),
        prepare_input=lambda adapter: _prepare_input(adapter, images, pdfs, office_docs),
    )

    # --- Исходные файлы пользователя -> R2 (ПОСЛЕ успешной генерации: упавший запрос не
    # оставляет объектов; сохраняются ОРИГИНАЛЫ, не уменьшенные копии). Сбой заливки не
    # теряет уже оплаченный ответ: сообщение сохраняется без этих вложений, причина — в
    # error_message лога.
    stored = await asyncio.gather(
        *(store_user_upload(user.user_id, session.id, f.filename, f.data, f.mime) for f in parsed),
        return_exceptions=True,
    )
    uploaded_records = [r for r in stored if isinstance(r, dict)]
    upload_failures = len(stored) - len(uploaded_records)
    extra_notes = (
        [f"Не сохранены вложения пользователя в R2: {upload_failures} из {len(stored)}"]
        if upload_failures
        else []
    )

    # --- История: только исходный текст + пометка о вложениях ----------
    bits: list[str] = []
    if images:
        bits.append(f"{len(images)} изображение(й)")
    if pdfs:
        bits.append(f"{len(pdfs)} PDF")
    if office_docs:
        bits.append(f"{len(office_docs)} документ(ов) docx/xlsx (текстом)")
    user_content = prompt
    if bits:
        user_content += (
            f"\n\n[Приложено: {', '.join(bits)}. "
            f"Вложения обработаны в этом запросе; при follow-up по истории модель их не видит.]"
        )

    return await persist_turn(
        db,
        user=user,
        session=session,
        started=started,
        user_content=user_content,
        user_attachments=[*key_attachments, *uploaded_records],
        outcome=outcome,
        extra_notes=extra_notes,
    )
