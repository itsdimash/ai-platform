"""Хранение запусков. RunStore — интерфейс (в тестах подменяется памятью),
PgRunStore — реализация поверх Postgres: каждая операция открывает собственную короткую
сессию БД (задача живёт дольше HTTP-запроса и не может использовать Depends(get_db))."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select, update

from app.db.session import SessionLocal
from app.models.chat import ChatMessage, ChatSession
from app.models.logs import AIRequestLog
from app.models.runs import TemplateRun
from app.services.chat_service import touch_session
from app.templates.errors import INTERRUPTED, TemplateHTTPError

ACTIVE = ("queued", "running")
TERMINAL = ("done", "failed", "cancelled")

INTERRUPTED_MESSAGE = "Сервис был перезапущен во время выполнения. Запустите шаблон заново."


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class RunRecord:
    id: str
    user_id: int
    user_role: str
    template_id: str
    session_id: int | None
    status: str
    progress: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    input_files: dict = field(default_factory=dict)
    result: dict | None = None
    error: dict | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    searches_used: int = 0
    created_at: datetime = field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunStore(ABC):
    @abstractmethod
    async def create(
        self,
        *,
        user_id: int,
        user_role: str,
        template_id: str,
        session_id: int | None,
        session_title: str,
        user_content: str,
        user_attachments: list[dict],
        params: dict,
        input_files: dict,
    ) -> RunRecord:
        """Сессия (если не задана) + сообщение пользователя + запись run (status=queued)."""

    @abstractmethod
    async def get(self, run_id: str) -> RunRecord | None: ...

    @abstractmethod
    async def count_active(self, user_id: int) -> int: ...

    @abstractmethod
    async def update(self, run_id: str, **fields) -> None:
        """Обновление полей записи (progress, status, started_at, ...)."""

    @abstractmethod
    async def finish(
        self,
        run_id: str,
        *,
        status: str,
        result: dict | None,
        error: dict | None,
        tokens_in: int,
        tokens_out: int,
        searches_used: int,
        assistant_text: str,
        table: list[dict] | None,
        attachments: list[dict],
        model_used: str | None,
        task_type: str,
    ) -> int | None:
        """Атомарно: сообщение ассистента в историю + терминальный статус run.
        Возвращает id сообщения. Если run уже в терминальном статусе — ничего не делает
        и возвращает None."""

    @abstractmethod
    async def log_request(self, **fields) -> None:
        """Запись в ai_request_logs."""

    @abstractmethod
    async def mark_interrupted(self, message_for: Callable[[str], str]) -> int:
        """queued/running -> failed (interrupted). Для каждого run с session_id в историю
        добавляется сообщение ассистента message_for(template_id). Возвращает число run."""


def _to_record(row: TemplateRun) -> RunRecord:
    return RunRecord(
        id=str(row.id),
        user_id=row.user_id,
        user_role=row.user_role,
        template_id=row.template_id,
        session_id=row.session_id,
        status=row.status,
        progress=row.progress or {},
        params=row.params or {},
        input_files=row.input_files or {},
        result=row.result,
        error=row.error,
        tokens_in=row.tokens_in or 0,
        tokens_out=row.tokens_out or 0,
        searches_used=row.searches_used or 0,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


class PgRunStore(RunStore):
    async def create(
        self,
        *,
        user_id,
        user_role,
        template_id,
        session_id,
        session_title,
        user_content,
        user_attachments,
        params,
        input_files,
    ) -> RunRecord:
        async with SessionLocal() as db:
            if session_id is not None:
                session = await db.get(ChatSession, session_id)
                if session is None or session.user_id != user_id:
                    raise TemplateHTTPError(404, "session_not_found", "Сессия не найдена")
            else:
                session = ChatSession(user_id=user_id, title=session_title[:200])
                db.add(session)
                await db.flush()
            db.add(
                ChatMessage(
                    session_id=session.id,
                    role="user",
                    content=user_content,
                    attachments=user_attachments,
                )
            )
            run = TemplateRun(
                id=uuid.uuid4(),
                user_id=user_id,
                user_role=user_role,
                template_id=template_id,
                session_id=session.id,
                status="queued",
                progress={"stage": "queued", "done": 0, "total": 0, "message": "В очереди"},
                params=params,
                input_files=input_files,
            )
            db.add(run)
            await touch_session(db, session.id)
            await db.commit()
            await db.refresh(run)
            return _to_record(run)

    async def get(self, run_id: str) -> RunRecord | None:
        uid = _parse_uuid(run_id)
        if uid is None:
            return None
        async with SessionLocal() as db:
            row = await db.get(TemplateRun, uid)
            return _to_record(row) if row else None

    async def count_active(self, user_id: int) -> int:
        async with SessionLocal() as db:
            result = await db.execute(
                select(func.count())
                .select_from(TemplateRun)
                .where(TemplateRun.user_id == user_id, TemplateRun.status.in_(ACTIVE))
            )
            return int(result.scalar_one())

    async def update(self, run_id: str, **fields) -> None:
        uid = _parse_uuid(run_id)
        if uid is None:
            return
        async with SessionLocal() as db:
            await db.execute(update(TemplateRun).where(TemplateRun.id == uid).values(**fields))
            await db.commit()

    async def finish(
        self,
        run_id,
        *,
        status,
        result,
        error,
        tokens_in,
        tokens_out,
        searches_used,
        assistant_text,
        table,
        attachments,
        model_used,
        task_type,
    ) -> int | None:
        uid = _parse_uuid(run_id)
        if uid is None:
            return None
        async with SessionLocal() as db:
            run = await db.get(TemplateRun, uid, with_for_update=True)
            if run is None or run.status in TERMINAL:
                return None
            message_id = None
            if run.session_id is not None:
                message = ChatMessage(
                    session_id=run.session_id,
                    role="ai",
                    content=assistant_text,
                    model_used=model_used,
                    task_type=task_type,
                    table_data=table,
                    attachments=attachments,
                )
                db.add(message)
                await db.flush()
                message_id = message.id
                await touch_session(db, run.session_id)
            if result is not None:
                result = {**result, "message_id": message_id}
            run.status = status
            run.result = result
            run.error = error
            run.tokens_in = tokens_in
            run.tokens_out = tokens_out
            run.searches_used = searches_used
            run.finished_at = utcnow()
            await db.commit()
            return message_id

    async def log_request(self, **fields) -> None:
        async with SessionLocal() as db:
            db.add(AIRequestLog(**fields))
            await db.commit()

    async def mark_interrupted(self, message_for: Callable[[str], str]) -> int:
        async with SessionLocal() as db:
            rows = (
                (
                    await db.execute(
                        select(TemplateRun).where(TemplateRun.status.in_(ACTIVE)).with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            now = utcnow()
            for run in rows:
                run.status = "failed"
                run.error = {"code": INTERRUPTED, "message": INTERRUPTED_MESSAGE}
                run.finished_at = now
                if run.session_id is not None:
                    db.add(
                        ChatMessage(
                            session_id=run.session_id,
                            role="ai",
                            content=message_for(run.template_id),
                            task_type=f"template:{run.template_id}",
                            attachments=[],
                        )
                    )
                    await touch_session(db, run.session_id)
            await db.commit()
            return len(rows)
