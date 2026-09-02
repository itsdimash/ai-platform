from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .validator import SQLValidationError, SQLValidator

_validator = SQLValidator()


async def run_db_query(
    sql: str,
    *,
    role: str,
    user_id: int,
    erp_session: AsyncSession,
) -> dict:
    """Валидирует и выполняет SQL, сгенерированный моделью, через read-only
    подключение к БД ERP. erp_session должен быть создан на пользователе
    ai_readonly (см. db/session.py) — это вторая линия защиты поверх
    валидатора: даже если валидатор что-то пропустит, у роли ai_readonly
    физически нет прав на запись."""

    try:
        validated = _validator.validate(sql, role=role, user_id=user_id)
    except SQLValidationError as e:
        return {"ok": False, "error": str(e)}

    result = await erp_session.execute(text(validated.sql))
    rows = [dict(row._mapping) for row in result.fetchall()]

    return {
        "ok": True,
        "sql_executed": validated.sql,
        "tables_used": validated.tables_used,
        "row_count": len(rows),
        "rows": rows,
    }
