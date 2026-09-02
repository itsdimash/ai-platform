from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import get_settings

settings = get_settings()

# БД самой платформы: логи, история чатов. Обычный read-write доступ.
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# Отдельное read-only подключение к БД ERP — используется ТОЛЬКО в db_query.
# Физически подключается под ai_readonly пользователем PostgreSQL, у которого
# нет прав на INSERT/UPDATE/DELETE — это вторая линия защиты поверх
# SQL-валидатора (см. db_query/validator.py).
erp_readonly_engine = (
    create_async_engine(settings.erp_readonly_database_url, pool_pre_ping=True)
    if settings.erp_readonly_database_url
    else None
)
ErpReadonlySessionLocal = (
    async_sessionmaker(erp_readonly_engine, expire_on_commit=False) if erp_readonly_engine else None
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def get_erp_readonly_db() -> AsyncGenerator[AsyncSession, None]:
    if ErpReadonlySessionLocal is None:
        raise RuntimeError("ERP_READONLY_DATABASE_URL не настроен")
    async with ErpReadonlySessionLocal() as session:
        yield session
