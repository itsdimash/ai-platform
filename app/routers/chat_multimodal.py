"""Мультимодальный чат: произвольный промпт + изображения / PDF / docx / xlsx.

Отдельный роут POST /v1/chat/multimodal — существующий JSON-роут
POST /v1/chat не трогается вообще (его контракт неизменен).

Отличия от /v1/chat:
- принимает multipart/form-data (prompt, session_id, model, files[], attachment_keys[]);
- если в запросе есть изображения ИЛИ PDF, который решено слать нативно,
  КЛАССИФИКАТОР ПРОПУСКАЕТСЯ полностью: модель = body.model, если задан,
  иначе дефолт gemini-flash. task_type / routing_rules при этом не
  участвуют (task_type в логе помечается как "multimodal");
- db_query в этом роуте не выполняется (запрос к БД + вложение — не наш кейс).

Что принимается (utils/uploads.py): png, jpg/jpeg, webp, pdf, docx, xlsx; до 10
файлов, изображение до 20 МБ, документ до 50 МБ, всё вместе до 100 МБ. Тип
определяется по расширению + magic bytes (Content-Type клиента не используется).
Файлы читаются кусками по 1 МБ с остановкой при превышении лимита.

Как вложения доходят до модели (utils/media.py):
- изображения — нативно; если не проходят по лимитам провайдера (Anthropic: 7,5 МБ
  сырых / 8000 px по стороне; общий бюджет запроса у всех провайдеров), провайдеру
  уходит уменьшенная копия (Pillow), а в R2 сохраняется оригинал;
- PDF — нативно у Claude/Gemini, если влезает в лимиты провайдера (размер, число
  страниц, бюджет запроса); иначе (и всегда у OpenAI) — текстовый слой, вклеенный в prompt;
- docx/xlsx — текст извлекается на бэкенде и вклеивается в prompt.

Хранение вложений: после УСПЕШНОЙ генерации исходные файлы сохраняются в R2 под
ai/{user_id}/{session_id}/uploads/... и привязываются к user-сообщению
(chat_messages.attachments), чтобы фронт восстановил их из истории. Модель при
follow-up по истории сами вложения не видит — история подмешивается текстом
(см. adapters/base.py), а base64 картинок/PDF туда намеренно не кладётся.
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
from ..classifier.classify import Classification, classify
from ..db.session import get_db
from ..limits import (
    MAX_ATTACHMENT_KEYS,
    MAX_EXTRACTED_CHARS_PER_FILE,
    MAX_EXTRACTED_CHARS_TOTAL,
    MAX_FILES_PER_REQUEST,
    MAX_TOTAL_UPLOAD_BYTES,
)
from ..models.chat import ChatMessage
from ..models.logs import AIRequestLog
from ..system_prompt import SYSTEM_PROMPT
from ..tools import ToolContext, apply_tool_calls
from ..utils.attachments import resolve_attachment_keys, store_user_upload, with_urls
from ..utils.media import (
    PROVIDER_ANTHROPIC,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    plan_media,
)
from ..utils.pdf import extract_pdf_pages, pdf_page_count
from ..utils.uploads import ParsedFile, classify_upload, read_upload
from .chat import (
    _build_contextual_prompt,
    _get_or_create_session,
    _get_recent_history,
    _route_engine,
    describe_failure,
    fail_request,
    touch_session,
)
from .deps import get_adapters
from .document_extract import _extract_docx, _extract_xlsx
from .schemas import ChatResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# Эвристика «скан / сложная вёрстка»: если текстовый слой даёт меньше
# символов на страницу, чем этот порог, — PDF почти наверняка скан или
# тяжёлая вёрстка, и pdfplumber-текст будет бесполезен.
PDF_SCAN_CHARS_PER_PAGE_THRESHOLD = 100

DEFAULT_MULTIMODAL_MODEL = "gemini-flash"  # когда body.model не задан


def _provider_of(adapter: ModelAdapter | None) -> str | None:
    if isinstance(adapter, AnthropicAdapter):
        return PROVIDER_ANTHROPIC
    if isinstance(adapter, GeminiAdapter):
        return PROVIDER_GEMINI
    if isinstance(adapter, OpenAIAdapter):
        return PROVIDER_OPENAI
    return None


def _supports_native_pdf(adapter: ModelAdapter | None) -> bool:
    """Claude и Gemini принимают PDF нативно; OpenAI Chat Completions — нет."""
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
    has_images = len(images) > 0

    # --- Роутинг ----------------------------------------------------------
    # Кандидат модели без классификатора: явный выбор пользователя или дефолт.
    candidate_model = body_model or DEFAULT_MULTIMODAL_MODEL
    send_pdf_native = bool(pdfs) and _supports_native_pdf(adapters.get(candidate_model))

    # Классификатор пропускается, если есть изображения ИЛИ PDF, который
    # решено слать нативно (см. ТЗ). Иначе (PDF + не-нативная модель, либо
    # запрос вообще без вложений) — обычная классификация, как в /v1/chat.
    skip_classifier = has_images or send_pdf_native

    session = await _get_or_create_session(db, sid, user.user_id, prompt)
    history_messages = await _get_recent_history(db, session.id)

    if skip_classifier:
        model_name = candidate_model
        task_type = "multimodal"
        confidence = 0.0
        used_fallback_confidence = False
        web_search = False
        needs_review = False
        max_tokens = _route_engine.default_max_tokens
    else:
        contextual_for_classify = _build_contextual_prompt(history_messages, prompt)
        try:
            classification = await classify(contextual_for_classify, adapters["gemini-flash"])
        except Exception:  # noqa: BLE001 — сбой классификатора не должен ронять запрос
            classification = Classification(
                task_type="general_qa", confidence=0.0, reasoning="classifier_call_failed"
            )
        task_type = classification.task_type
        confidence = classification.confidence
        if body_model:
            rule = _route_engine.rule_for(task_type)
            model_name = body_model
            web_search = rule.get("web_search", False)
            needs_review = rule.get("require_human_review", False)
            used_fallback_confidence = False
            max_tokens = rule.get("max_tokens", _route_engine.default_max_tokens)
        else:
            decision = _route_engine.decide(task_type, confidence)
            model_name = decision.model
            web_search = decision.web_search
            needs_review = decision.require_human_review
            used_fallback_confidence = decision.used_fallback_confidence
            max_tokens = decision.max_tokens

    if model_name not in adapters:
        await fail_request(
            db,
            user_id=user.user_id,
            session_id=session.id,
            session_created=sid is None,
            task_type=task_type,
            confidence=confidence,
            used_fallback_confidence=used_fallback_confidence,
            model_used=model_name,
            started=started,
            status_code=400,
            detail=f"Неизвестная модель: {model_name!r}",
            error_message=f"Неизвестная модель: {model_name!r}",
        )

    adapter = adapters[model_name]
    # В model_used пишем РЕАЛЬНЫЙ ID модели провайдера (см. routers/chat.py).
    model_id = adapter.model
    provider = _provider_of(adapter) or PROVIDER_OPENAI

    # --- Подготовка вложений под лимиты провайдера ---------------------------
    # Pillow / pdfplumber — CPU-тяжёлые, при файлах до 50 МБ их нельзя гонять в
    # event loop. Ошибки валидации (HTTPException) пробрасываются как 400.
    pdf_pages = [await asyncio.to_thread(_safe_page_count, p.data) for p in pdfs]
    try:
        plan = await asyncio.to_thread(
            plan_media,
            provider,
            images=images,
            pdfs=pdfs,
            pdf_pages=pdf_pages,
            is_200k_context="haiku" in model_id.lower(),
        )
    except HTTPException as exc:
        # Сессия создана этим запросом — не оставляем пустой чат.
        await fail_request(
            db,
            user_id=user.user_id,
            session_id=session.id,
            session_created=sid is None,
            task_type=task_type,
            confidence=confidence,
            used_fallback_confidence=used_fallback_confidence,
            model_used=model_id,
            started=started,
            status_code=exc.status_code,
            detail=str(exc.detail),
            error_message=str(exc.detail)[:500],
        )
    if plan.resized_names:
        logger.info("images downscaled for %s: %d", provider, len(plan.resized_names))

    attachments: list[Attachment] = [
        Attachment(mime_type=img.mime, data=img.data, kind="image") for img in plan.images
    ]
    attachments += [
        Attachment(mime_type="application/pdf", data=pdf.data, kind="pdf_document")
        for pdf in plan.native_pdfs
    ]

    # Текстовый путь: PDF вне лимитов провайдера (и все PDF у OpenAI), docx, xlsx.
    budget = _TextBudget()
    prompt_suffix_parts: list[str] = []
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
        prompt_suffix_parts.append(
            f"\n\n=== Содержимое PDF «{pdf.filename}»{note}{marker} ===\n{snippet}"
        )

    for doc in office_docs:
        extractor = _extract_docx if doc.kind == "docx" else _extract_xlsx
        try:
            text = await asyncio.to_thread(extractor, doc.data)
        except Exception:  # noqa: BLE001 — битый документ не должен ронять весь запрос
            text = ""
        snippet, was_truncated = budget.take(text)
        marker = " [обрезано]" if was_truncated else ""
        label = "DOCX" if doc.kind == "docx" else "XLSX"
        prompt_suffix_parts.append(
            f"\n\n=== Содержимое {label} «{doc.filename}»{marker} ===\n{snippet}"
        )

    final_prompt = prompt + "".join(prompt_suffix_parts)
    contextual_prompt = _build_contextual_prompt(history_messages, final_prompt)

    # --- Вызов модели ---------------------------------------------------
    try:
        result = await adapter.generate(
            prompt=contextual_prompt,
            system=SYSTEM_PROMPT,
            web_search=web_search,
            max_tokens=max_tokens,
            attachments=attachments or None,
        )
        # Файловые tools исполняются здесь (сбой -> ToolExecutionError -> 5xx).
        result = await apply_tool_calls(
            result,
            ctx=ToolContext(user_id=user.user_id, session_id=session.id),
            prompt=contextual_prompt,
        )
    except Exception as exc:  # noqa: BLE001 — любая ошибка провайдера/билдера -> 5xx + запись в лог
        status_code, detail, error_message = describe_failure(exc)
        await fail_request(
            db,
            user_id=user.user_id,
            session_id=session.id,
            session_created=sid is None,
            task_type=task_type,
            confidence=confidence,
            used_fallback_confidence=used_fallback_confidence,
            model_used=model_id,
            started=started,
            status_code=status_code,
            detail=detail,
            error_message=error_message,
        )
    text_out = result.text
    ai_attachments = result.attachments
    tokens_in, tokens_out = result.tokens_in, result.tokens_out

    # --- Исходные файлы пользователя -> R2 (ПОСЛЕ успешной генерации: упавший
    # запрос не оставляет объектов; сохраняются ОРИГИНАЛЫ, не уменьшенные копии).
    # Сбой заливки не теряет уже оплаченный ответ: сообщение сохраняется без этих
    # вложений, причина — в error_message лога.
    stored = await asyncio.gather(
        *(store_user_upload(user.user_id, session.id, f.filename, f.data, f.mime) for f in parsed),
        return_exceptions=True,
    )
    uploaded_records = [r for r in stored if isinstance(r, dict)]
    upload_failures = len(stored) - len(uploaded_records)
    user_attachments = [*key_attachments, *uploaded_records]
    notes: list[str] = []
    if upload_failures:
        notes.append(f"Не сохранены вложения пользователя в R2: {upload_failures} из {len(stored)}")
    if plan.resized_names:
        notes.append(f"Изображений уменьшено для провайдера: {len(plan.resized_names)}")
    warning = "; ".join(notes) or None

    latency_ms = int((time.monotonic() - started) * 1000)

    # --- История: только исходный текст + пометка о вложениях ----------
    attach_note_bits: list[str] = []
    if images:
        attach_note_bits.append(f"{len(images)} изображение(й)")
    if plan.native_pdfs:
        attach_note_bits.append(f"{len(plan.native_pdfs)} PDF (нативно)")
    if plan.text_pdfs:
        attach_note_bits.append(f"{len(plan.text_pdfs)} PDF (текстом)")
    if office_docs:
        attach_note_bits.append(f"{len(office_docs)} документ(ов) docx/xlsx (текстом)")

    user_content = prompt
    if attach_note_bits:
        user_content += (
            f"\n\n[Приложено: {', '.join(attach_note_bits)}. "
            f"Вложения обработаны в этом запросе; при follow-up по истории модель их не видит.]"
        )

    db.add(
        ChatMessage(
            session_id=session.id,
            role="user",
            content=user_content,
            attachments=user_attachments,
        )
    )
    db.add(
        ChatMessage(
            session_id=session.id,
            role="ai",
            content=text_out,
            model_used=model_id,
            task_type=task_type,
            attachments=ai_attachments,
        )
    )
    await touch_session(db, session.id)
    db.add(
        AIRequestLog(
            user_id=user.user_id,
            session_id=session.id,
            task_type=task_type,
            confidence=confidence,
            used_fallback_confidence=used_fallback_confidence,
            model_used=model_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            status="success",
            error_message=warning,
        )
    )
    await db.commit()

    return ChatResponse(
        session_id=session.id,
        text=text_out,
        task_type=task_type,
        model_used=model_id,
        confidence=confidence,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        table=None,
        needs_review=needs_review,
        attachments=with_urls(ai_attachments),
    )
