"""Единый конвейер хода чата для /v1/chat и /v1/chat/multimodal:

    история -> классификатор -> роутер -> генерация (принудительные tools) -> tools
    -> сохранение / лог / ответ,   либо fail_request при любой ошибке.

Принудительный tool (категории presentation/document/spreadsheet/pdf/image): модели,
принимающие tool_choice (Gemini ANY, OpenAI, Claude Sonnet 5 / Opus 5, Haiku без
thinking), вызываются с ним сразу. Claude Sonnet 5.5 / Opus 5.5 / Fable отвергают
принудительный tool_choice (400), поэтому для них попытка 1 — auto + подсказка в
системном промпте. Если инструмент не вызван, делается ровно одна повторная попытка
(принудительная без thinking, а для моделей без tool_choice — жёсткая инструкция),
в логе ставится forced_fallback=true. Нет вызова и после неё -> generation_failed.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.base import (
    FINISH_MAX_TOKENS,
    FINISH_REFUSAL,
    Attachment,
    ChatTurn,
    GenerationResult,
    ModelAdapter,
    same_model,
)
from ..auth import CurrentUser
from ..classifier.classify import Classification, classify
from ..db_query.chat_sql import _handle_db_query
from ..errors import GenerationError, GenerationFailed, ModelRefusal, OutputTruncated
from ..models.chat import ChatMessage, ChatSession
from ..models.logs import AIRequestLog
from ..router.route import RouteDecision, Router, load_config
from ..routers.schemas import ChatResponse
from ..system_prompt import build_system_prompt, hard_tool_instruction, tool_hint
from ..tools import ALL_TOOLS, ToolContext, ToolExecutionError, apply_tool_calls
from ..utils.attachments import with_urls
from .history import build_turns, glue_context, with_user_message

logger = logging.getLogger(__name__)

route_engine = Router()
_FILE_TOOL_NAMES = {t["name"] for t in ALL_TOOLS}

FILE_ERROR_DETAIL = "Не удалось сформировать файл. Попробуйте ещё раз."
PROVIDER_ERROR_DETAIL = (
    "Не удалось обработать запрос: сервис модели недоступен. Попробуйте ещё раз."
)


# --- Подготовка вложений и результат хода --------------------------------------------


@dataclass
class PreparedInput:
    """Что multimodal-роут подготовил под выбранного провайдера."""

    attachments: list[Attachment] = field(default_factory=list)
    prompt_suffix: str = ""  # извлечённый текст документов, вклеиваемый в промпт
    notes: list[str] = field(default_factory=list)


PrepareFn = Callable[[ModelAdapter], Awaitable[PreparedInput]]


@dataclass
class TurnOutcome:
    text: str
    table: list[dict] | None
    ai_attachments: list[dict]
    tokens_in: int
    tokens_out: int
    model_used: str  # фактически использованная модель (по ответу провайдера)
    image_model: str | None
    classification: Classification
    decision: RouteDecision
    forced_fallback: bool
    notes: list[str]


# --- Ошибки ---------------------------------------------------------------------------


def failure_for(exc: Exception) -> tuple[int, str | dict, str]:
    """(HTTP-статус, detail для клиента, текст для лога). В detail никогда не попадает
    текст исключения — он идёт только в ai_request_logs.error_message."""
    if isinstance(exc, GenerationError):
        return 502, exc.detail, f"{exc.code}: {exc.internal}"[:500]
    if isinstance(exc, ToolExecutionError):
        return (
            500,
            FILE_ERROR_DETAIL,
            f"{type(exc.cause).__name__} в {exc.tool_name}: {exc.cause}"[:500],
        )
    if isinstance(exc, HTTPException):
        return exc.status_code, exc.detail, str(exc.detail)[:500]
    return 502, PROVIDER_ERROR_DETAIL, f"{type(exc).__name__}: {exc}"[:500]


async def fail_request(
    db: AsyncSession,
    *,
    user_id: int,
    session_id: int,
    session_created: bool,
    task_type: str,
    confidence: float,
    used_fallback_confidence: bool,
    model_used: str,
    started: float,
    status_code: int,
    detail: str | dict,
    error_message: str,
    classifier_tokens_in: int = 0,
    classifier_tokens_out: int = 0,
    forced_fallback: bool = False,
) -> NoReturn:
    """Фиксирует сбой запроса и отвечает ошибкой.

    Сбой НЕ попадает в историю чата: ни ответ ассистента, ни сообщение пользователя не
    сохраняются. Если сессия была создана этим же запросом — откатываем её (пустой чат
    в списке не нужен); тогда в логе session_id=None. Реальная причина пишется только
    в ai_request_logs.error_message.
    """
    if session_created:
        await db.rollback()
        log_session_id = None
    else:
        log_session_id = session_id

    db.add(
        AIRequestLog(
            user_id=user_id,
            session_id=log_session_id,
            task_type=task_type,
            confidence=confidence,
            used_fallback_confidence=used_fallback_confidence,
            model_used=model_used,
            tokens_in=0,
            tokens_out=0,
            latency_ms=int((time.monotonic() - started) * 1000),
            classifier_tokens_in=classifier_tokens_in,
            classifier_tokens_out=classifier_tokens_out,
            forced_fallback=forced_fallback,
            status="error",
            error_message=error_message,
        )
    )
    await db.commit()
    raise HTTPException(status_code=status_code, detail=detail)


def today_local() -> date:
    """Текущая дата для системного промпта в часовом поясе из config.yaml -> timezone
    (по умолчанию UTC; на slim-образе без tzdata неизвестный пояс тихо заменяется на UTC)."""
    try:
        tz = ZoneInfo(load_config().get("timezone", "UTC"))
    except ZoneInfoNotFoundError:
        tz = UTC
    return datetime.now(tz).date()


# --- Сессии -----------------------------------------------------------------------------


async def get_or_create_session(
    db: AsyncSession, session_id: int | None, user_id: int, first_prompt: str
) -> ChatSession:
    if session_id is not None:
        session = await db.get(ChatSession, session_id)
        if session is None or session.user_id != user_id:
            raise HTTPException(status_code=404, detail="Сессия не найдена")
        return session

    title = first_prompt[:60] + ("…" if len(first_prompt) > 60 else "")
    session = ChatSession(user_id=user_id, title=title)
    db.add(session)
    await db.flush()  # получить session.id до commit
    return session


async def touch_session(db: AsyncSession, session_id: int) -> None:
    """Обновляет ChatSession.updated_at при добавлении сообщений (ORM onupdate
    срабатывает только при изменении самой строки сессии)."""
    await db.execute(
        update(ChatSession).where(ChatSession.id == session_id).values(updated_at=func.now())
    )


# --- Генерация с принудительными tools ------------------------------------------------------


def _check_finish(result: GenerationResult, *, tool_expected: bool) -> None:
    if result.finish == FINISH_REFUSAL:
        raise ModelRefusal(f"refusal: {result.finish_detail}")
    if result.finish == FINISH_MAX_TOKENS and (tool_expected or result.tool_calls):
        raise OutputTruncated(f"max_tokens (out={result.tokens_out})")


def _called_file_tool(result: GenerationResult) -> bool:
    return any(call.get("name") in _FILE_TOOL_NAMES for call in result.tool_calls)


async def generate_with_tools(
    adapter: ModelAdapter,
    *,
    turns: list[ChatTurn],
    system: str,
    decision: RouteDecision,
    attachments: list[Attachment] | None,
) -> tuple[GenerationResult, bool]:
    """Возвращает (результат, forced_fallback). См. описание конвейера в начале модуля."""
    common = {
        "messages": turns,
        "max_tokens": decision.max_tokens,
        "attachments": attachments or None,
        "web_search": decision.web_search,
        "timeout_s": decision.timeout_s,
    }
    if decision.web_search:
        common["tools"] = []  # поиску файловые инструменты не нужны (и это ~1,5k токенов)

    tool = decision.forced_tool
    if not tool:
        result = await adapter.generate(system=system, thinking=decision.thinking, **common)
        _check_finish(result, tool_expected=False)
        return result, False

    caps = adapter.caps
    # --- попытка 1 ---
    force = tool if caps.can_force_tool(decision.thinking) else None
    thinking = "off" if force and caps.forced_tool == "no_thinking" else decision.thinking
    first = await adapter.generate(
        system=system if force else system + tool_hint(tool),
        force_tool=force,
        thinking=thinking,
        **common,
    )
    _check_finish(first, tool_expected=True)
    if _called_file_tool(first):
        return first, False

    # --- попытка 2 (forced_fallback) ---
    logger.warning("tool %s not called by %s, retrying once", tool, adapter.model)
    if caps.forced_tool != "unsupported":
        second = await adapter.generate(system=system, force_tool=tool, thinking="off", **common)
    else:
        second = await adapter.generate(
            system=system + tool_hint(tool) + hard_tool_instruction(tool),
            thinking="low",
            **common,
        )
    second.tokens_in += first.tokens_in
    second.tokens_out += first.tokens_out
    second.latency_ms += first.latency_ms
    _check_finish(second, tool_expected=True)
    if _called_file_tool(second):
        return second, True
    raise GenerationFailed(
        f"tool {tool} not called after 2 attempts by {adapter.model} "
        f"(finish={second.finish}, text={second.text[:80]!r})"
    )


# --- Ход чата ------------------------------------------------------------------------------


async def run_turn(
    *,
    db: AsyncSession,
    adapters: dict[str, ModelAdapter],
    user: CurrentUser,
    session: ChatSession,
    session_created: bool,
    started: float,
    user_text: str,
    model_choice: str | None,
    history_rows: list[ChatMessage],
    attachment_kinds: str = "",
    prepare_input: PrepareFn | None = None,
) -> TurnOutcome:
    """Классификация -> роутинг -> генерация. Любая ошибка завершается fail_request."""
    config = load_config()
    history_cfg = config["history"]
    classifier_cfg = config["classifier"]

    # Явный выбор модели проверяем до трат на классификацию.
    if model_choice and model_choice not in adapters:
        await fail_request(
            db,
            user_id=user.user_id,
            session_id=session.id,
            session_created=session_created,
            task_type="unknown",
            confidence=0.0,
            used_fallback_confidence=False,
            model_used=model_choice,
            started=started,
            status_code=400,
            detail=f"Неизвестная модель: '{model_choice}'.",
            error_message=f"Неизвестная модель: {model_choice!r}",
        )

    context = build_turns(
        history_rows,
        max_messages=history_cfg["max_messages"],
        max_tokens=history_cfg["max_tokens_estimate"],
        chars_per_token=history_cfg["chars_per_token"],
    )

    # 1. Классификация — дешёвая быстрая модель. Сбой не роняет запрос: безопасный
    # дефолт general_qa с уверенностью 0.0 (роутер отправит его на сильную модель).
    try:
        classification = await classify(
            user_text,
            adapters[classifier_cfg["model"]],
            context=context,
            attachment_kinds=attachment_kinds,
            max_tokens=classifier_cfg["max_tokens"],
            timeout_s=classifier_cfg["timeout_s"],
            max_exchanges=classifier_cfg["max_exchanges"],
            assistant_chars=classifier_cfg["assistant_chars"],
        )
    except Exception:
        logger.warning("classifier failed", exc_info=True)
        classification = Classification("general_qa", 0.0, "classifier_call_failed")

    # 2. Роутинг. С вложениями SQL-путь не используется: db_query -> правило general_qa.
    has_attachments = bool(attachment_kinds)
    routed_type = classification.task_type
    if routed_type == "db_query" and has_attachments:
        routed_type = "general_qa"
    if model_choice:
        decision = route_engine.decide_explicit(
            routed_type, classification.confidence, model_choice
        )
    else:
        decision = route_engine.decide(routed_type, classification.confidence)
    adapter = adapters[decision.model]

    common_fail = {
        "db": db,
        "user_id": user.user_id,
        "session_id": session.id,
        "session_created": session_created,
        "task_type": classification.task_type,
        "confidence": classification.confidence,
        "used_fallback_confidence": decision.used_fallback_confidence,
        "started": started,
        "classifier_tokens_in": classification.tokens_in,
        "classifier_tokens_out": classification.tokens_out,
    }

    notes: list[str] = []
    forced_fallback = False
    table: list[dict] | None = None
    image_model: str | None = None
    ai_attachments: list[dict] = []
    model_used = adapter.model

    try:
        if routed_type == "db_query" and not decision.used_fallback_confidence:
            # SQL-путь: код перенесён дословно в db_query/chat_sql.py; whitelist, валидатор
            # и read-only выполнение не затронуты.
            text, table, tokens_in, tokens_out = await _handle_db_query(
                glue_context(history_rows, user_text), user, adapter
            )
        else:
            prepared = await prepare_input(adapter) if prepare_input else PreparedInput()
            notes += prepared.notes
            turns = with_user_message(context, user_text + prepared.prompt_suffix)
            system = build_system_prompt(user.role, today_local())
            result, forced_fallback = await generate_with_tools(
                adapter,
                turns=turns,
                system=system,
                decision=decision,
                attachments=prepared.attachments,
            )
            # Файловые tools исполняются здесь (сбой -> ToolExecutionError -> 5xx).
            result = await apply_tool_calls(
                result,
                ctx=ToolContext(user_id=user.user_id, session_id=session.id),
                prompt=user_text,
            )
            text, tokens_in, tokens_out = result.text, result.tokens_in, result.tokens_out
            ai_attachments, image_model = result.attachments, result.image_model
            model_used = result.model_used or adapter.model
            if not same_model(adapter.model, model_used):
                notes.append(f"fallback: {adapter.model} -> {model_used}")
    except Exception as exc:  # noqa: BLE001 — любая ошибка -> лог + ответ с кодом
        status_code, detail, error_message = failure_for(exc)
        await fail_request(
            model_used=model_used,
            status_code=status_code,
            detail=detail,
            error_message=error_message,
            forced_fallback=forced_fallback,
            **common_fail,
        )

    return TurnOutcome(
        text=text,
        table=table,
        ai_attachments=ai_attachments,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        model_used=model_used,
        image_model=image_model,
        classification=classification,
        decision=decision,
        forced_fallback=forced_fallback,
        notes=notes,
    )


async def persist_turn(
    db: AsyncSession,
    *,
    user: CurrentUser,
    session: ChatSession,
    started: float,
    user_content: str,
    user_attachments: list[dict],
    outcome: TurnOutcome,
    extra_notes: list[str] | None = None,
) -> ChatResponse:
    """Сохраняет оба сообщения, лог, обновляет сессию и собирает ответ API."""
    latency_ms = int((time.monotonic() - started) * 1000)
    notes = [*outcome.notes, *(extra_notes or [])]
    clf = outcome.classification

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
            content=outcome.text,
            model_used=outcome.model_used,
            task_type=clf.task_type,
            table_data=outcome.table,
            attachments=outcome.ai_attachments,
            image_model=outcome.image_model,
        )
    )
    await touch_session(db, session.id)
    db.add(
        AIRequestLog(
            user_id=user.user_id,
            session_id=session.id,
            task_type=clf.task_type,
            confidence=clf.confidence,
            used_fallback_confidence=outcome.decision.used_fallback_confidence,
            model_used=outcome.model_used,
            tokens_in=outcome.tokens_in,
            tokens_out=outcome.tokens_out,
            latency_ms=latency_ms,
            classifier_tokens_in=clf.tokens_in,
            classifier_tokens_out=clf.tokens_out,
            forced_fallback=outcome.forced_fallback,
            image_model=outcome.image_model,
            status="success",
            error_message="; ".join(notes)[:500] or None,
        )
    )
    await db.commit()

    return ChatResponse(
        session_id=session.id,
        text=outcome.text,
        task_type=clf.task_type,
        model_used=outcome.model_used,
        confidence=clf.confidence,
        tokens_in=outcome.tokens_in,
        tokens_out=outcome.tokens_out,
        latency_ms=latency_ms,
        table=outcome.table,
        needs_review=outcome.decision.require_human_review,
        attachments=with_urls(outcome.ai_attachments),
        image_model_used=outcome.image_model,
    )
