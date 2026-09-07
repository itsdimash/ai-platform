"""Мультимодальный чат: произвольный промпт + изображения / PDF, которые
модель обрабатывает НАТИВНО (vision, document blocks), а не через
pdfplumber-extraction.

Отдельный роут POST /v1/chat/multimodal — существующий JSON-роут
POST /v1/chat не трогается вообще (его контракт неизменен).

Отличия от /v1/chat:
- принимает multipart/form-data (prompt, session_id, model, files[]);
- если в запросе есть изображения ИЛИ PDF, который решено слать нативно,
  КЛАССИФИКАТОР ПРОПУСКАЕТСЯ полностью: модель = body.model, если задан,
  иначе дефолт gemini-flash. task_type / routing_rules при этом не
  участвуют (task_type в логе помечается как "multimodal");
- db_query в этом роуте не выполняется (запрос к БД + вложение — не
  наш кейс); PDF, отправленный не нативно (GPT-модель), конвертируется
  в текст и вклеивается в prompt здесь, на бэкенде.

Хранение вложений: НЕ сохраняются ни на диск, ни в БД — живут только в
памяти на время обработки запроса. В chat_messages.content пишется
только исходный текст пользователя + пометка «было приложено N вложений».
ИЗВЕСТНОЕ ОГРАНИЧЕНИЕ: при follow-up вопросах по истории модель эти вложения
больше не видит — история подмешивается текстом (см. adapters/base.py),
а base64 картинок/PDF туда намеренно не кладётся.

docx/xlsx этот роут не обрабатывает — они остаются на старом пути
POST /v1/documents/extract + вклейка текста фронтом. Комбинация
«docx-текст (уже в prompt) + фото (attachment)» в одном запросе
поддерживается: текст приходит строкой в prompt, изображения — файлами.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.base import Attachment, ModelAdapter
from ..auth import CurrentUser, get_current_user
from ..classifier.classify import Classification, classify
from ..db.session import get_db
from ..models.chat import ChatMessage
from ..models.logs import AIRequestLog
from .chat import (
    _build_contextual_prompt,
    _get_or_create_session,
    _get_recent_history,
    _route_engine,
)
from .deps import get_adapters
from .schemas import ChatResponse

router = APIRouter()

# --- Лимиты и валидация (обязательно на бэкенде, фронту не доверяем) --------

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 МБ на одно изображение
MAX_IMAGES_PER_MESSAGE = 4
MAX_TOTAL_ATTACHMENT_BYTES = 15 * 1024 * 1024  # суммарно по всем вложениям запроса

# Провайдерские лимиты inline-передачи PDF (без Files API), для справки —
# общий потолок запроса (15 МБ выше) в любом случае жёстче:
#   Anthropic  : ~32 МБ и до 100 страниц на документ;
#   Gemini     : суммарный размер запроса с inline_data < 20 МБ.
ANTHROPIC_INLINE_PDF_MAX_BYTES = 32 * 1024 * 1024
GEMINI_INLINE_REQUEST_MAX_BYTES = 20 * 1024 * 1024

MAX_EXTRACTED_PDF_CHARS = 20_000  # как в document_extract.py — не взрываем бюджет токенов

# Эвристика «скан / сложная вёрстка»: если текстовый слой даёт меньше
# символов на страницу, чем этот порог, — PDF почти наверняка скан или
# тяжёлая вёрстка, и pdfplumber-текст будет бесполезен.
PDF_SCAN_CHARS_PER_PAGE_THRESHOLD = 100

# Логические модели из MODEL_FACTORY, умеющие нативный PDF (document block).
# OpenAI Chat Completions нативного PDF не умеет — для неё PDF идёт текстом.
_NATIVE_PDF_MODELS = {
    "claude-haiku",
    "claude-sonnet",
    "claude-opus",
    "gemini-flash",
    "gemini-pro",
}

DEFAULT_MULTIMODAL_MODEL = "gemini-flash"  # когда body.model не задан


def _supports_native_pdf(model_name: str) -> bool:
    return model_name in _NATIVE_PDF_MODELS


def _sniff_mime(content: bytes) -> str | None:
    """Определяет тип по magic bytes (не по расширению). Возвращает один из
    image/png, image/jpeg, image/webp, application/pdf либо None для всего
    остального."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def _looks_like_svg(content: bytes) -> bool:
    head = content[:512].lstrip().lower()
    return head.startswith(b"<?xml") or b"<svg" in head


class _ParsedFile:
    __slots__ = ("data", "filename", "mime")

    def __init__(self, filename: str, data: bytes, mime: str):
        self.filename = filename
        self.data = data
        self.mime = mime


async def _read_and_validate(
    files: list[UploadFile] | None,
) -> tuple[list[_ParsedFile], list[_ParsedFile]]:
    """Читает файлы в память, валидирует по magic bytes и лимитам.
    Возвращает (images, pdfs). Любое нарушение — HTTP 400."""
    images: list[_ParsedFile] = []
    pdfs: list[_ParsedFile] = []
    total = 0

    for upload in files or []:
        raw = await upload.read()
        if not raw:
            continue

        total += len(raw)
        if total > MAX_TOTAL_ATTACHMENT_BYTES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Суммарный размер вложений превышает 15 МБ",
            )

        name = upload.filename or "attachment"

        if _looks_like_svg(raw):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"SVG не поддерживается: {name!r}",
            )

        mime = _sniff_mime(raw)
        if mime is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"Неподдерживаемый тип файла {name!r}. Разрешены: PNG, JPEG, WebP, PDF",
            )

        if mime == "application/pdf":
            pdfs.append(_ParsedFile(name, raw, mime))
        else:
            if len(raw) > MAX_IMAGE_BYTES:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"Изображение {name!r} больше 5 МБ",
                )
            images.append(_ParsedFile(name, raw, mime))
            if len(images) > MAX_IMAGES_PER_MESSAGE:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail="Не больше 4 изображений в одном сообщении",
                )

    return images, pdfs


def _extract_pdf_text_and_pages(content: bytes) -> tuple[str, int]:
    """Текстовый слой PDF (склеенный через "\\n") + число страниц с текстом —
    для эвристики «скан vs текстовый PDF». Парсинг — общий
    app.utils.pdf.extract_pdf_pages (то же, что и в document_extract)."""
    from app.utils.pdf import extract_pdf_pages

    pages = extract_pdf_pages(content)
    return "\n".join(pages), len(pages)


def _parse_session_id(session_id: str | None) -> int | None:
    if session_id is None or session_id.strip() in ("", "null", "undefined"):
        return None
    try:
        return int(session_id)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="session_id должен быть целым числом"
        ) from exc


@router.post("/v1/chat/multimodal", response_model=ChatResponse)
async def chat_multimodal(
    prompt: str = Form(...),
    session_id: str | None = Form(None),
    model: str | None = Form(None),
    files: list[UploadFile] | None = File(None),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    adapters: dict[str, ModelAdapter] = Depends(get_adapters),
) -> ChatResponse:
    started = time.monotonic()

    body_model = model.strip() if model and model.strip() else None
    sid = _parse_session_id(session_id)

    images, pdfs = await _read_and_validate(files)
    has_images = len(images) > 0

    # --- Роутинг ----------------------------------------------------------
    # Кандидат модели без классификатора: явный выбор пользователя или дефолт.
    candidate_model = body_model or DEFAULT_MULTIMODAL_MODEL
    send_pdf_native = bool(pdfs) and _supports_native_pdf(candidate_model)

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
            used_fallback_confidence = False
        else:
            decision = _route_engine.decide(task_type, confidence)
            model_name = decision.model
            web_search = decision.web_search
            used_fallback_confidence = decision.used_fallback_confidence

    if model_name not in adapters:
        _latency = int((time.monotonic() - started) * 1000)
        db.add(ChatMessage(session_id=session.id, role="user", content=prompt))
        db.add(
            AIRequestLog(
                user_id=user.user_id,
                session_id=session.id,
                task_type=task_type,
                confidence=confidence,
                used_fallback_confidence=used_fallback_confidence,
                model_used=model_name,
                tokens_in=0,
                tokens_out=0,
                latency_ms=_latency,
                status="error",
                error_message=f"Неизвестная модель: {model_name!r}",
            )
        )
        await db.commit()
        raise HTTPException(status_code=400, detail=f"Неизвестная модель: {model_name!r}")

    # --- Сборка вложений и итогового промпта -----------------------------
    attachments: list[Attachment] = [
        Attachment(mime_type=img.mime, data=img.data, kind="image") for img in images
    ]

    prompt_suffix_parts: list[str] = []
    native_pdf_count = 0
    for pdf in pdfs:
        try:
            text, pages = _extract_pdf_text_and_pages(pdf.data)
        except Exception:  # noqa: BLE001 — битый PDF не должен ронять весь запрос
            text, pages = "", 0

        chars_per_page = len(text) / pages if pages else 0.0
        scan_like = pages == 0 or chars_per_page < PDF_SCAN_CHARS_PER_PAGE_THRESHOLD

        if _supports_native_pdf(model_name):
            # Claude / Gemini — всегда нативно, качество не зависит от
            # текстового слоя.
            attachments.append(
                Attachment(mime_type="application/pdf", data=pdf.data, kind="pdf_document")
            )
            native_pdf_count += 1
        else:
            # OpenAI: нативного PDF нет. Дефолт из ТЗ — fallback на
            # pdfplumber-текст с пометкой о возможном низком качестве.
            # TODO: чтобы поддержать сканы в GPT, рендерить страницы PDF
            # в PNG и слать их как image-вложения (pdf2image / pymupdf).
            snippet = text[:MAX_EXTRACTED_PDF_CHARS]
            note = (
                " (низкое качество: похоже на скан/сложную вёрстку, "
                "для таких файлов выберите Claude или Gemini)"
                if scan_like
                else ""
            )
            truncated = " [обрезано]" if len(text) > MAX_EXTRACTED_PDF_CHARS else ""
            prompt_suffix_parts.append(
                f"\n\n=== Содержимое PDF «{pdf.filename}»{note}{truncated} ===\n{snippet}"
            )

    final_prompt = prompt + "".join(prompt_suffix_parts)
    contextual_prompt = _build_contextual_prompt(history_messages, final_prompt)

    # --- Вызов модели ---------------------------------------------------
    status_str = "success"
    error_message = None
    try:
        result = await adapters[model_name].generate(
            prompt=contextual_prompt,
            web_search=web_search,
            max_tokens=2048,
            attachments=attachments or None,
        )
        text_out = result.text
        tokens_in, tokens_out = result.tokens_in, result.tokens_out
    except Exception as exc:  # noqa: BLE001 — любая ошибка идёт в лог, а не 500 без следа
        status_str = "error"
        error_message = str(exc)[:500]
        text_out = "Не удалось обработать запрос. Попробуйте ещё раз."
        tokens_in = tokens_out = 0

    latency_ms = int((time.monotonic() - started) * 1000)

    # --- История: только исходный текст + пометка о вложениях ----------
    attach_note_bits: list[str] = []
    if images:
        attach_note_bits.append(f"{len(images)} изображение(й)")
    if native_pdf_count:
        attach_note_bits.append(f"{native_pdf_count} PDF (нативно)")
    text_pdf_count = len(pdfs) - native_pdf_count
    if text_pdf_count:
        attach_note_bits.append(f"{text_pdf_count} PDF (текстом)")

    user_content = prompt
    if attach_note_bits:
        user_content += (
            f"\n\n[Приложено: {', '.join(attach_note_bits)}. "
            f"Вложения обработаны в этом запросе; при follow-up по истории модель их не видит.]"
        )

    db.add(ChatMessage(session_id=session.id, role="user", content=user_content))
    db.add(
        ChatMessage(
            session_id=session.id,
            role="ai",
            content=text_out,
            model_used=model_name,
            task_type=task_type,
        )
    )
    db.add(
        AIRequestLog(
            user_id=user.user_id,
            session_id=session.id,
            task_type=task_type,
            confidence=confidence,
            used_fallback_confidence=used_fallback_confidence,
            model_used=model_name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            status=status_str,
            error_message=error_message,
        )
    )
    await db.commit()

    return ChatResponse(
        session_id=session.id,
        text=text_out,
        task_type=task_type,
        model_used=model_name,
        confidence=confidence,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        table=None,
    )
