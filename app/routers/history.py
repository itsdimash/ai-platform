from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser, get_current_user
from ..db.session import get_db
from ..models.chat import ChatMessage, ChatSession
from ..utils.attachments import with_urls
from .schemas import SessionRename

router = APIRouter()


@router.get("/v1/sessions")
async def list_sessions(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user.user_id)
        .order_by(ChatSession.updated_at.desc())
        .limit(50)
    )
    sessions = result.scalars().all()
    return [
        {"id": s.id, "title": s.title, "updated_at": s.updated_at.isoformat()} for s in sessions
    ]


@router.get("/v1/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(ChatSession, session_id)
    if session is None or session.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Сессия не найдена")

    # created_at у пары user/ai одинаков (одна транзакция, now() = начало
    # транзакции), поэтому порядок добиваем по id.
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
    )
    messages = result.scalars().all()
    return [
        {
            "role": m.role,
            "content": m.content,
            "model_used": m.model_used,
            "task_type": m.task_type,
            "created_at": m.created_at.isoformat(),
            "table": m.table_data,
            "attachments": with_urls(m.attachments),
            "image_model_used": m.image_model,
        }
        for m in messages
    ]


@router.patch("/v1/sessions/{session_id}")
async def rename_session(
    session_id: int,
    body: SessionRename,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Переименовать свою сессию. updated_at не меняется — чат не должен
    подниматься в списке из-за смены названия."""
    session = await db.get(ChatSession, session_id)
    if session is None or session.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Сессия не найдена")

    await db.execute(
        update(ChatSession)
        .where(ChatSession.id == session_id)
        .values(title=body.title, updated_at=ChatSession.updated_at)
    )
    await db.commit()
    await db.refresh(session)
    return {"id": session.id, "title": session.title, "updated_at": session.updated_at.isoformat()}


@router.delete("/v1/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Удалить сессию чата вместе со всеми сообщениями.

    ВАЖНО: cascade="all, delete-orphan" на ChatSession.messages — это
    ORM-каскад SQLAlchemy, а НЕ ondelete="CASCADE" на уровне БД (у
    ChatMessage.session_id обычный ForeignKey без этого флага). Поэтому
    удалять нужно строго через db.delete(session) (ORM), а не через
    bulk delete()-запрос — иначе сообщения осиротеют и останутся в
    таблице chat_messages без родительской сессии.

    Файлы в R2 (префикс ai/) при этом НЕ удаляются.
    """
    session = await db.get(ChatSession, session_id)
    if session is None or session.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Сессия не найдена")

    await db.delete(session)
    await db.commit()
