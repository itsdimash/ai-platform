import re
import time
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.base import ModelAdapter
from ..auth import CurrentUser, get_current_user
from ..classifier.classify import Classification, classify
from ..db.session import ErpReadonlySessionLocal, get_db
from ..db_query.executor import run_db_query
from ..db_query.schema_context import build_schema_context
from ..models.chat import ChatMessage, ChatSession
from ..models.logs import AIRequestLog
from ..router.route import RouteDecision, Router
from ..system_prompt import SYSTEM_PROMPT
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

    # 0. Сессия и история — резолвим/создаём сессию ДО классификации и
    # генерации (раньше это делалось только в конце, для сохранения), чтобы
    # успеть подмешать предыдущие сообщения в контекст. Без этого шага
    # follow-up вопросы вроде "покажи то же самое, но с именами" обрабатывались
    # так, будто это первое сообщение в диалоге — ни классификатор, ни модель
    # не видели, к чему относится "то же самое".
    #
    # Адаптеры (adapters/base.py) сейчас принимают только один prompt: str,
    # без структуры messages/history — поэтому история подмешивается текстом,
    # а не отдельными "турнами" в API провайдера. В историю сохраняется
    # ОРИГИНАЛЬНЫЙ body.prompt (не contextual_prompt) — иначе контекст
    # задваивался бы на каждом следующем сообщении.
    session = await _get_or_create_session(db, body.session_id, user.user_id, body.prompt)
    history_messages = await _get_recent_history(db, session.id)
    contextual_prompt = _build_contextual_prompt(history_messages, body.prompt)

    # 1. Классификация — всегда через дешёвую быструю модель.
    # Сбой классификатора (таймаут, rate limit и т.п.) не должен ронять
    # запрос без следа в логе — безопасный fallback на general_qa.
    try:
        classification = await classify(contextual_prompt, adapters["gemini-flash"])
    except Exception:  # noqa: BLE001 — намеренно широкий catch, см. комментарий выше
        classification = Classification(task_type="general_qa", confidence=0.0, reasoning="classifier_call_failed")

    # 2. Роутинг — выбор целевой модели по конфигу, если пользователь сам не
    # указал модель явно (body.model). Ручной выбор полностью обходит
    # auto-роутинг (включая fallback по низкой уверенности) — пользователь
    # уже принял решение, но флаги задачи (web_search, require_human_review)
    # из routing_rules всё равно применяются к его выбору.
    if body.model:
        if body.model not in adapters:
            raise HTTPException(
                status_code=400,
                detail=f"Неизвестная модель: '{body.model}'.",
            )
        rule = _route_engine.rule_for(classification.task_type)
        decision = RouteDecision(
            model=body.model,
            web_search=rule.get("web_search", False),
            require_human_review=rule.get("require_human_review", False),
            used_fallback_confidence=False,
            max_tokens=rule.get("max_tokens", _route_engine.default_max_tokens),
        )
    else:
        decision = _route_engine.decide(classification.task_type, classification.confidence)

    # Опечатка/рассинхрон в config.yaml (модель не зарегистрирована в
    # адаптерах) — явная ошибка вместо сырого KeyError глубже в коде.
    if decision.model not in adapters:
        status = "error"
        error_message = f"Модель '{decision.model}' из конфига роутера не зарегистрирована в adapters."
        latency_ms = int((time.monotonic() - started) * 1000)
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
                contextual_prompt, user, adapters[decision.model]
            )
        else:
            adapter = adapters[decision.model]
            result = await adapter.generate(
                prompt=contextual_prompt,
                system=SYSTEM_PROMPT,
                web_search=decision.web_search,
                max_tokens=decision.max_tokens,
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

    # 4. История чатов — сохраняем оба сообщения (сессия уже резолвлена в
    # шаге 0). ВАЖНО: content=body.prompt, а не contextual_prompt — в базу
    # идёт только то, что реально написал пользователь.
    db.add(ChatMessage(session_id=session.id, role="user", content=body.prompt))
    db.add(
        ChatMessage(
            session_id=session.id,
            role="ai",
            content=text_out,
            model_used=decision.model,
            task_type=classification.task_type,
            table_data=table_result,
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


_HISTORY_MESSAGES_LIMIT = 20  # ~10 предыдущих обменов (user+ai) для контекста
# Компромисс между "помнит достаточно для follow-up вопросов" и стоимостью:
# каждое сообщение в истории пересчитывается ЗАНОВО и в классификаторе,
# и в основной генерации на КАЖДЫЙ следующий вопрос в сессии — это не
# разовые токены, а повторяющиеся на каждом шаге диалога. Для db_query
# это дёшево (в историю пишется только "Найдено строк: N"), но для
# general_qa/translation/summarization реплики могут быть длинными —
# при заметном росте счёта за API стоит уменьшить это число обратно.


async def _get_recent_history(db: AsyncSession, session_id: int) -> list[ChatMessage]:
    """Последние сообщения сессии (до текущего хода), для контекста
    классификатора и модели. Порядок — от старых к новым (для чтения)."""
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(_HISTORY_MESSAGES_LIMIT)
    )
    return list(reversed(result.scalars().all()))


def _build_contextual_prompt(history: list[ChatMessage], new_prompt: str) -> str:
    """Склеивает недавнюю историю сообщений с новым вопросом в одну строку.

    Адаптеры (см. adapters/base.py) сейчас принимают только plain-строку
    prompt, без параметра messages/history — контекст передаётся текстом,
    а не структурированными "турнами" в API провайдера. Для новой сессии
    (history пуста) ничего не меняет — не плодим лишний текст там, где он
    не нужен.
    """
    if not history:
        return new_prompt

    lines = ["Предыдущий контекст переписки:"]
    for message in history:
        speaker = "Пользователь" if message.role == "user" else "Ассистент"
        lines.append(f"{speaker}: {message.content}")
    lines.append("")
    lines.append(f"Новый вопрос пользователя: {new_prompt}")
    return "\n".join(lines)


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
        f"Верни ТОЛЬКО SQL, без пояснений, без markdown.\n\n"
        f"Требования к результату (он показывается пользователю как таблица, "
        f"колонки называются ровно так, как ты их назовёшь в SELECT):\n"
        f'- Давай каждой колонке человекочитаемый алиас на русском, например '
        f'`name AS "Название"`, `price AS "Цена"`, `quantity AS "Количество"`.\n'
        f"- Не включай технические/служебные колонки в результат, если пользователь "
        f"явно не попросил их показать: id, любые *_id как внешние ключи, а также "
        f"служебные метки времени (created_at, updated_at и подобные) — они не несут "
        f"пользы конечному пользователю.\n"
        f"- Если нужна связанная сущность (например категория товара), делай JOIN "
        f'и выводи её название, а не *_id (например `c.name AS "Категория"`), '
        f"а не сырой category_id.\n"
        f'- Столбцы с датой/временем приводи через `::date AS "Дата"` '
        f"(или `to_char(col, 'DD.MM.YYYY')`), если пользователь не просил точное время.\n\n"
        f"Вопрос: {prompt}"
    )
    result = await adapter.generate(prompt=sql_prompt, max_tokens=1500)
    generated_sql = _extract_sql(result.text)
    generated_sql = _strip_hidden_columns(generated_sql, prompt)

    async with ErpReadonlySessionLocal() as erp_session:
        query_result = await run_db_query(
            generated_sql, role=user.role, user_id=user.user_id, erp_session=erp_session
        )

    if not query_result["ok"]:
        return (f"Не могу выполнить этот запрос: {query_result['error']}", None, result.tokens_in, result.tokens_out)

    text_out = f"Найдено строк: {query_result['row_count']}"
    safe_rows = _sanitize_rows(query_result["rows"])
    return (text_out, safe_rows, result.tokens_in, result.tokens_out)


def _sanitize_value(value):
    """Приводит значение из сырого результата SQL-запроса к JSON-совместимому
    виду. datetime/date из asyncpg приходят как объекты Python, а не строки —
    штатный json.dumps (используемый SQLAlchemy для JSONB-колонок) падает на
    них с TypeError при db.commit(). Decimal сюда же — тоже не сериализуется
    напрямую и типично встречается в колонках price/amount."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _sanitize_rows(rows: list[dict] | None) -> list[dict] | None:
    if rows is None:
        return None
    return [{key: _sanitize_value(val) for key, val in row.items()} for row in rows]


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


# Служебные колонки, которые пользователю обычно не нужны в общей выдаче —
# вырезаются из SELECT после генерации SQL, а не просьбой в промпте: модель
# (особенно gemini-flash) следует текстовой инструкции "не выбирай X"
# ненадёжно, поэтому этот фильтр — детерминированный бэкстоп, а не
# единственная линия защиты (инструкция в sql_prompt остаётся первой
# линией — так модель реже вообще их генерирует, и реже приходится резать).
_HIDDEN_COLUMN_KEYS = {"id", "created_at", "updated_at", "inserted_at", "modified_at"}
_HIDDEN_COLUMN_SUFFIX = "_id"

# Если пользователь явно упомянул одно из этих слов в вопросе — считаем,
# что колонку он таки просил, и не вырезаем её несмотря на попадание в
# список выше.
_ID_REQUEST_HINTS = ("id", "идентификатор", "айди")
_DATE_REQUEST_HINTS = ("дата", "созда", "created", "время", "когда")


def _split_top_level(s: str, sep: str) -> list[str]:
    """Разбивает строку по `sep`, игнорируя вхождения внутри скобок и кавычек
    (нужно, чтобы не порезать список колонок SELECT по запятой внутри
    функций вроде COUNT(a, b) или CASE WHEN ...)."""
    parts: list[str] = []
    depth = 0
    in_single = in_double = False
    current: list[str] = []
    for ch in s:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        if not in_single and not in_double:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == sep and depth == 0:
                parts.append("".join(current))
                current = []
                continue
        current.append(ch)
    parts.append("".join(current))
    return parts


def _find_top_level_keyword(sql: str, keyword: str, start: int = 0) -> int:
    """Ищет позицию ключевого слова (SELECT/FROM) на верхнем уровне
    вложенности скобок — чтобы не зацепить FROM внутри подзапроса."""
    depth = 0
    in_single = in_double = False
    n = len(sql)
    klen = len(keyword)
    i = start
    while i < n:
        ch = sql[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        if not in_single and not in_double:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and sql[i : i + klen].upper() == keyword.upper():
                before_ok = i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")
                after_ok = i + klen >= n or not (sql[i + klen].isalnum() or sql[i + klen] == "_")
                if before_ok and after_ok:
                    return i
        i += 1
    return -1


def _column_source_key(expr: str) -> str:
    """Из выражения колонки SELECT ('p.created_at', 'created_at::date',
    'c.name AS "Категория"') достаёт исходное имя колонки без алиаса,
    таблицы-префикса и приведения типа — по нему и матчим на служебность."""
    alias_match = re.search(r'\bAS\b\s+(?:"[^"]+"|\'[^\']+\'|[A-Za-z_][A-Za-z0-9_]*)', expr, re.IGNORECASE)
    source = expr[: alias_match.start()] if alias_match else expr
    source = re.sub(r"::\w+\s*$", "", source).strip()
    source = source.rsplit(".", 1)[-1] if "." in source else source
    return source.strip('"').strip("'").strip().lower()


def _strip_hidden_columns(sql: str, user_prompt: str) -> str:
    """Детерминированно вырезает служебные колонки (id, *_id, created_at,
    updated_at) из сгенерированного SELECT, если пользователь явно их не
    просил. Работает поверх готового SQL, а не полагается на то, что модель
    сама учла инструкцию в промпте."""
    select_pos = _find_top_level_keyword(sql, "SELECT")
    from_pos = _find_top_level_keyword(sql, "FROM", start=select_pos + 6) if select_pos != -1 else -1
    if select_pos == -1 or from_pos == -1:
        return sql  # не похоже на обычный SELECT ... FROM — не трогаем

    select_clause = sql[select_pos + 6 : from_pos]
    columns = _split_top_level(select_clause, ",")
    if len(columns) <= 1:
        return sql  # единственная колонка — резать нечего, иначе получим пустой SELECT

    prompt_lower = user_prompt.lower()
    wants_id = any(hint in prompt_lower for hint in _ID_REQUEST_HINTS)
    wants_dates = any(hint in prompt_lower for hint in _DATE_REQUEST_HINTS)

    kept = []
    for col in columns:
        key = _column_source_key(col)
        is_hidden_id = key == "id" or key.endswith(_HIDDEN_COLUMN_SUFFIX)
        is_hidden_date = key in _HIDDEN_COLUMN_KEYS
        if is_hidden_id and not wants_id:
            continue
        if is_hidden_date and not wants_dates:
            continue
        kept.append(col)

    if not kept:
        return sql  # если бы вырезали всё — лучше вернуть как есть, чем сломать запрос

    return sql[: select_pos + 6] + " " + ", ".join(c.strip() for c in kept) + " " + sql[from_pos:]


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
