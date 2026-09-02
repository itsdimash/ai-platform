from fastapi import APIRouter, Depends, HTTPException
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
        }
        for m in messages
    ]
