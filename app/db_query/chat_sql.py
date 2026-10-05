"""SQL-путь чата (db_query): модель генерирует SELECT по схеме, доступной роли
пользователя, валидатор (SQLValidator) проверяет и переписывает запрос, выполнение
идёт через read-only подключение к БД ERP.

Код ПЕРЕНЕСЁН из routers/chat.py дословно, без изменения логики (whitelist, валидатор
и read-only выполнение лежат в соседних модулях и не затронуты). Вынесено, чтобы
сервис чата (services/chat_service.py) мог его использовать без циклического
импорта роутера.
"""

import re
from datetime import date, datetime
from decimal import Decimal

from ..adapters.base import ModelAdapter
from ..auth import CurrentUser
from ..db.session import ErpReadonlySessionLocal
from .executor import run_db_query
from .schema_context import build_schema_context


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
        f"- Давай каждой колонке человекочитаемый алиас на русском, например "
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
    # tools=[] обязателен: без него adapter.generate() цепляет файловые tools,
    # и модель могла ответить вызовом generate_spreadsheet вместо SQL.
    result = await adapter.generate(prompt=sql_prompt, max_tokens=1500, tools=[])
    generated_sql = _extract_sql(result.text)
    generated_sql = _strip_hidden_columns(generated_sql, prompt)

    async with ErpReadonlySessionLocal() as erp_session:
        query_result = await run_db_query(
            generated_sql, role=user.role, user_id=user.user_id, erp_session=erp_session
        )

    if not query_result["ok"]:
        return (
            f"Не могу выполнить этот запрос: {query_result['error']}",
            None,
            result.tokens_in,
            result.tokens_out,
        )

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
    alias_match = re.search(
        r'\bAS\b\s+(?:"[^"]+"|\'[^\']+\'|[A-Za-z_][A-Za-z0-9_]*)', expr, re.IGNORECASE
    )
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
    from_pos = (
        _find_top_level_keyword(sql, "FROM", start=select_pos + 6) if select_pos != -1 else -1
    )
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
