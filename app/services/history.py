"""История чата для модели: реплики с ролями вместо склейки в одну строку.

- роль БД «ai» (и «ai-clarify») -> assistant;
- последние N сообщений (config.yaml -> history.max_messages, по умолчанию 40);
- страховочная обрезка с начала по ОЦЕНКЕ токенов (символы / chars_per_token);
- сообщения с вложениями получают короткую пометку («[создан файл: name]»), чтобы
  модель знала, что файл уже создан/приложен;
- подряд идущие реплики одной роли склеиваются, ведущие реплики ассистента отбрасываются
  (провайдеры требуют, чтобы диалог начинался с пользователя).
"""

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.base import ChatTurn
from ..models.chat import ChatMessage

DEFAULT_MAX_MESSAGES = 40
DEFAULT_MAX_TOKENS = 120_000
DEFAULT_CHARS_PER_TOKEN = 2.5
_USER_NOTE_MARKER = "[Приложено:"  # пометка, которую multimodal-роут уже пишет в content


class _Row(Protocol):
    role: str
    content: str
    attachments: list[dict] | None


def estimate_tokens(text: str, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    """Грубая оценка числа токенов (без токенайзера): для русского ~2,5 символа/токен."""
    return int(len(text) / chars_per_token) + 1


def map_role(db_role: str) -> str:
    return "user" if db_role == "user" else "assistant"


def _attachment_note(role: str, attachments: Sequence[dict]) -> str:
    names = ", ".join(a.get("name", "файл") for a in attachments)
    if role == "user":
        return f"[приложено: {names}]"
    images = all(a.get("type") == "image" for a in attachments)
    return f"[создано изображение: {names}]" if images else f"[создан файл: {names}]"


def row_to_turn(row: _Row) -> ChatTurn | None:
    role = map_role(row.role)
    content = (row.content or "").strip()
    attachments = row.attachments or []
    if attachments and not (role == "user" and _USER_NOTE_MARKER in content):
        note = _attachment_note(role, attachments)
        content = f"{content}\n{note}" if content else note
    return ChatTurn(role, content) if content else None  # type: ignore[arg-type]


def merge_consecutive(turns: list[ChatTurn]) -> list[ChatTurn]:
    merged: list[ChatTurn] = []
    for turn in turns:
        if merged and merged[-1].role == turn.role:
            merged[-1] = ChatTurn(turn.role, f"{merged[-1].content}\n\n{turn.content}")
        else:
            merged.append(turn)
    return merged


def _drop_leading_assistant(turns: list[ChatTurn]) -> list[ChatTurn]:
    while turns and turns[0].role == "assistant":
        turns = turns[1:]
    return turns


def build_turns(
    rows: Sequence[_Row],
    *,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
) -> list[ChatTurn]:
    """Строки БД (от старых к новым) -> реплики для провайдера."""
    turns = [t for t in (row_to_turn(r) for r in list(rows)[-max_messages:]) if t]
    turns = _drop_leading_assistant(merge_consecutive(turns))
    while (
        len(turns) > 1
        and sum(estimate_tokens(t.content, chars_per_token) for t in turns) > max_tokens
    ):
        turns = _drop_leading_assistant(turns[1:])
    return turns


def with_user_message(turns: list[ChatTurn], text: str) -> list[ChatTurn]:
    """Добавляет текущее сообщение пользователя (склеивая с предыдущим user-ходом, если он есть)."""
    return merge_consecutive([*turns, ChatTurn("user", text)])


async def fetch_recent_rows(db: AsyncSession, session_id: int, limit: int) -> list[ChatMessage]:
    """Последние сообщения сессии (до текущего хода), от старых к новым. Порядок
    добит по id: у пары user/ai created_at одинаков (одна транзакция)."""
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


def glue_context(rows: Sequence[_Row], new_prompt: str, max_messages: int = 6) -> str:
    """Короткая текстовая склейка контекста — только для SQL-пути (db_query), где
    запрос к модели строится одной строкой со схемой БД."""
    recent = list(rows)[-max_messages:]
    if not recent:
        return new_prompt
    lines = ["Предыдущий контекст переписки:"]
    for row in recent:
        speaker = "Пользователь" if row.role == "user" else "Ассистент"
        lines.append(f"{speaker}: {row.content}")
    lines += ["", f"Новый вопрос пользователя: {new_prompt}"]
    return "\n".join(lines)
