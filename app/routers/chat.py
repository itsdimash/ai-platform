import re
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.base import ModelAdapter
from ..auth import CurrentUser, get_current_user
from ..classifier.classify import Classification, classify
from ..config import get_settings
from ..db.session import ErpReadonlySessionLocal, get_db
from ..db_query.executor import run_db_query
from ..db_query.schema_context import build_schema_context
from ..models.chat import ChatMessage, ChatSession
from ..models.logs import AIRequestLog
from ..router.route import Router
from .deps import get_adapters
from .schemas import ChatRequest, ChatResponse

router = APIRouter()
_route_engine = Router()


@router.post("/v1/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    adapters: dict[str, ModelAdapter] = Depends(get_adapters),
) -> ChatResponse:
    started = time.monotonic()

    # 1. Классификация — всегда через дешёвую быструю модель.
    # Сбой классификатора (таймаут, rate limit и т.п.) не должен ронять
    # запрос без следа в логе — безопасный fallback на general_qa.
    try:
        classification = await classify(body.prompt, adapters["gemini-flash"])
    except Exception:  # noqa: BLE001 — намеренно широкий catch, см. комментарий выше
        classification = Classification(task_type="general_qa", confidence=0.0, reasoning="classifier_call_failed")

    # 2. Роутинг — выбор целевой модели по конфигу
    decision = _route_engine.decide(classification.task_type, classification.confidence)

    # Опечатка/рассинхрон в config.yaml (модель не зарегистрирована в
    # адаптерах) — явная ошибка вместо сырого KeyError глубже в коде.
    if decision.model not in adapters:
        status = "error"
        error_message = f"Модель '{decision.model}' из конфига роутера не зарегистрирована в adapters."
        latency_ms = int((time.monotonic() - started) * 1000)
        session = await _get_or_create_session(db, body.session_id, user.user_id, body.prompt)
        db.add(ChatMessage(session_id=session.id, role="user", content=body.prompt))
        db.add(
            AIRequestLog(
                user_id=user.user_id,
                session_id=session.id,
                task_type=classification.task_type,
                confidence=classification.confidence,
                used_fallback_confidence=decision.used_fallback_confidence,
                model_used=decision.model,
                tokens_in=0,
                tokens_out=0,
                latency_ms=latency_ms,
                status=status,
                error_message=error_message,
            )
        )
        await db.commit()
        raise HTTPException(status_code=500, detail="Ошибка конфигурации модели. Мы уже знаем об этом.")

    # 3. Выполнение задачи
    table_result: list[dict] | None = None
    status = "success"
    error_message = None

    try:
        if classification.task_type == "db_query" and not decision.used_fallback_confidence:
            text_out, table_result, tokens_in, tokens_out = await _handle_db_query(
                body.prompt, user, adapters[decision.model]
            )
        else:
            adapter = adapters[decision.model]
            result = await adapter.generate(
                prompt=body.prompt,
                web_search=decision.web_search,
                max_tokens=2048,
            )
            text_out = result.text
            tokens_in, tokens_out = result.tokens_in, result.tokens_out
    except Exception as e:  # noqa: BLE001 — намеренно широкий catch: любая ошибка
        # должна попасть в лог, а не уронить запрос без следа
        status = "error"
        error_message = str(e)[:500]
        text_out = "Не удалось обработать запрос. Попробуйте ещё раз."
        tokens_in = tokens_out = 0

    latency_ms = int((time.monotonic() - started) * 1000)

    # 4. История чатов — сохраняем сессию и оба сообщения
    session = await _get_or_create_session(db, body.session_id, user.user_id, body.prompt)
    db.add(ChatMessage(session_id=session.id, role="user", content=body.prompt))
    db.add(
        ChatMessage(
            session_id=session.id,
            role="ai",
            content=text_out,
            model_used=decision.model,
            task_type=classification.task_type,
        )
    )

    # 5. Логирование — с первого дня, не после
    db.add(
        AIRequestLog(
            user_id=user.user_id,
            session_id=session.id,
            task_type=classification.task_type,
            confidence=classification.confidence,
            used_fallback_confidence=decision.used_fallback_confidence,
            model_used=decision.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            status=status,
            error_message=error_message,
        )
    )
    await db.commit()

    return ChatResponse(
        session_id=session.id,
        text=text_out,
        task_type=classification.task_type,
        model_used=decision.model,
        confidence=classification.confidence,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        table=table_result,
    )


async def _handle_db_query(
    prompt: str, user: CurrentUser, adapter: ModelAdapter
) -> tuple[str, list[dict] | None, int, int]:
    """Просит модель сгенерировать SQL по схеме, доступной роли пользователя,
    валидирует и выполняет через read-only подключение к БД ERP."""

    if ErpReadonlySessionLocal is None:
        return ("Доступ к данным компании ещё не настроен.", None, 0, 0)

    schema_context = build_schema_context(user.role)

    sql_prompt = (
        f"{schema_context}\n\n"
        f"Сгенерируй один SELECT-запрос PostgreSQL, отвечающий на вопрос пользователя, "
        f"используя ТОЛЬКО перечисленные выше таблицы и колонки. "
        f"Верни ТОЛЬКО SQL, без пояснений, без markdown.\n\nВопрос: {prompt}"
    )
    result = await adapter.generate(prompt=sql_prompt, max_tokens=500)
    generated_sql = _extract_sql(result.text)

    async with ErpReadonlySessionLocal() as erp_session:
        query_result = await run_db_query(
            generated_sql, role=user.role, user_id=user.user_id, erp_session=erp_session
        )

    if not query_result["ok"]:
        return (f"Не могу выполнить этот запрос: {query_result['error']}", None, result.tokens_in, result.tokens_out)

    text_out = f"Найдено строк: {query_result['row_count']}"
    return (text_out, query_result["rows"], result.tokens_in, result.tokens_out)


_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_SELECT_RE = re.compile(r"SELECT\b.*?(?:;|\Z)", re.IGNORECASE | re.DOTALL)


def _extract_sql(raw_text: str) -> str:
    """Достаёт SQL из ответа модели независимо от того, обернула ли модель
    его в markdown-код (в любом регистре: ```sql, ```SQL, ```) или просто
    добавила текст до/после запроса вопреки инструкции."""

    text = raw_text.strip()

    fence_match = _SQL_FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1).strip()

    select_match = _SELECT_RE.search(text)
    if select_match:
        return select_match.group(0).rstrip(";").strip()

    return text.strip("`").strip()


async def _get_or_create_session(
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
