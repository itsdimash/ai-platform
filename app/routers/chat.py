import time

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.base import ModelAdapter
from ..auth import CurrentUser, get_current_user
from ..db.session import get_db
from ..router.route import load_config
from ..services.chat_service import get_or_create_session, persist_turn, run_turn
from ..services.history import fetch_recent_rows
from ..utils.attachments import describe_kinds, kinds_of_records, resolve_attachment_keys
from .deps import get_adapters
from .schemas import ChatRequest, ChatResponse

router = APIRouter()


@router.post("/v1/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    adapters: dict[str, ModelAdapter] = Depends(get_adapters),
) -> ChatResponse:
    """Ход чата (JSON). Весь конвейер — в services/chat_service.py: классификация,
    роутинг по router/config.yaml, генерация (с принудительными tools для файловых
    категорий), сохранение. Сбой возвращает ошибку и не попадает в историю."""
    started = time.monotonic()

    # Ключи ранее загруженных файлов проверяем ДО создания сессии и до трат на модель.
    # Только привязка: по ключам проверяется владелец и существование, текст в промпт
    # не извлекается (клиент вклеивает текст документа в prompt сам).
    user_attachments = await resolve_attachment_keys(body.attachment_keys, user.user_id)

    session = await get_or_create_session(db, body.session_id, user.user_id, body.prompt)
    history_rows = await fetch_recent_rows(db, session.id, load_config()["history"]["max_messages"])

    outcome = await run_turn(
        db=db,
        adapters=adapters,
        user=user,
        session=session,
        session_created=body.session_id is None,
        started=started,
        user_text=body.prompt,
        model_choice=body.model,
        history_rows=history_rows,
        attachment_kinds=describe_kinds(kinds_of_records(user_attachments)),
    )
    return await persist_turn(
        db,
        user=user,
        session=session,
        started=started,
        user_content=body.prompt,
        user_attachments=user_attachments,
        outcome=outcome,
    )
