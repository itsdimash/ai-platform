from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser, get_current_user
from ..db.session import get_db
from ..models.chat import ChatMessage, ChatSession

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

    result = await db.execute(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at)
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
        }
        for m in messages
    ]


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
    """
    session = await db.get(ChatSession, session_id)
    if session is None or session.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Сессия не найдена")

    await db.delete(session)
    await db.commit()
