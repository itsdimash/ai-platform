"""История чата для модели: реплики с ролями вместо склейки в одну строку.

- роль БД «ai» (и «ai-clarify») -> assistant;
- последние N сообщений (config.yaml -> history.max_messages, по умолчанию 40);
- страховочная обрезка с начала по ОЦЕНКЕ токенов (символы / chars_per_token);
- сообщения с вложениями получают короткую пометку («[создан файл: name]»), чтобы
  модель знала, что файл уже создан/приложен;
- у самых свежих презентаций/изображений ассистента (поле `spec` вложения) в реплику
  добавляются исходные данные файла — так модель может ПРАВИТЬ файл, а не создавать новый;
- подряд идущие реплики одной роли склеиваются, ведущие реплики ассистента отбрасываются
  (провайдеры требуют, чтобы диалог начинался с пользователя).
"""

import json
from collections.abc import Sequence
from typing import NamedTuple, Protocol

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.base import ChatTurn
from ..models.chat import ChatMessage

DEFAULT_MAX_MESSAGES = 40
DEFAULT_MAX_TOKENS = 120_000
DEFAULT_CHARS_PER_TOKEN = 2.5
_USER_NOTE_MARKER = "[Приложено:"  # пометка, которую multimodal-роут уже пишет в content

# Исходные данные файлов для правок: сколько последних файлов каждого вида показывать модели
# и потолок размера одной записи (больше — слишком дорого, правка пойдёт «по описанию»).
SPEC_PER_KIND = 2
MAX_SPEC_CHARS = 60_000
MAX_EDIT_SPEC_CHARS = 150_000  # для файла, выбранного кнопкой «Изменить» (потолок выше)


class SpecInfo(NamedTuple):
    kind: str  # вид файла для группировки («две последние презентации»)
    tool: str  # инструмент, который его создаёт и правит
    task_type: str  # категория роутинга


_SPEC_BY_MIME = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": SpecInfo(
        "deck", "generate_presentation", "presentation"
    ),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": SpecInfo(
        "doc", "generate_document", "document"
    ),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": SpecInfo(
        "sheet", "generate_spreadsheet", "spreadsheet"
    ),
    "application/pdf": SpecInfo("pdf", "generate_pdf", "pdf"),
}
_IMAGE_SPEC = SpecInfo("image", "generate_image", "image")


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


def spec_info(att: dict) -> SpecInfo | None:
    """Что за файл и чем его править; None — исходных данных нет (старый файл) или вид
    файла не поддерживает правки."""
    if not isinstance(att.get("spec"), dict):
        return None
    if att.get("type") == "image":
        return _IMAGE_SPEC
    return _SPEC_BY_MIME.get(att.get("mime", ""))


def _spec_json(att: dict) -> str:
    return json.dumps(att["spec"], ensure_ascii=False, separators=(",", ":"))


def _spec_note(att: dict, tool: str) -> str:
    name = att.get("name", "файл")
    return f"[Исходные данные файла «{name}» для правок (параметры {tool}):\n{_spec_json(att)}]"


def edit_block(att: dict) -> str:
    """Явная пометка к сообщению пользователя, который выбрал файл кнопкой «Изменить»:
    исходные данные файла + что с ними делать. Вне зависимости от окна истории."""
    info = spec_info(att)
    name = att.get("name", "файл")
    version = f" (версия v{int(att.get('revision') or 1)})"
    if not info:
        return f"[Пользователь правит файл «{name}»{version}, но исходных данных нет.]"
    data = _spec_json(att)
    if len(data) > MAX_EDIT_SPEC_CHARS:
        body = "Исходные данные слишком велики и не приложены: воссоздай файл по описанию из чата."
    else:
        body = f"Исходные данные файла (параметры {info.tool}):\n{data}"
    return (
        f"[Пользователь правит файл «{name}»{version}. {body}\n"
        f"Вызови {info.tool} один раз с ПОЛНЫМ обновлённым содержимым: всё, что пользователь "
        f"не просил менять, оставь без изменений; в revises укажи «{name}».]"
    )


def _spec_targets(
    rows: Sequence[_Row], exclude_keys: frozenset[str] = frozenset()
) -> set[tuple[int, int]]:
    """Какие вложения (индекс строки, индекс вложения) получат исходные данные: по
    SPEC_PER_KIND самых свежих файлов ассистента каждого вида, не крупнее MAX_SPEC_CHARS.
    exclude_keys — файлы, чьи данные уже приложены к сообщению пользователя (edit_block)."""
    taken: dict[str, int] = {}
    targets: set[tuple[int, int]] = set()
    for r_idx in range(len(rows) - 1, -1, -1):
        row = rows[r_idx]
        if map_role(row.role) != "assistant":
            continue
        atts = row.attachments or []
        for a_idx in range(len(atts) - 1, -1, -1):
            info = spec_info(atts[a_idx])
            if not info or atts[a_idx].get("key") in exclude_keys:
                continue
            if taken.get(info.kind, 0) >= SPEC_PER_KIND:
                continue
            if len(_spec_json(atts[a_idx])) > MAX_SPEC_CHARS:
                continue
            taken[info.kind] = taken.get(info.kind, 0) + 1
            targets.add((r_idx, a_idx))
    return targets


def row_to_turn(row: _Row, spec_indexes: frozenset[int] = frozenset()) -> ChatTurn | None:
    """spec_indexes — индексы вложений строки, для которых нужно добавить исходные данные."""
    role = map_role(row.role)
    content = (row.content or "").strip()
    attachments = row.attachments or []
    if attachments and not (role == "user" and _USER_NOTE_MARKER in content):
        note = _attachment_note(role, attachments)
        content = f"{content}\n{note}" if content else note
    for idx in sorted(spec_indexes):
        info = spec_info(attachments[idx])
        if info:
            content = f"{content}\n{_spec_note(attachments[idx], info.tool)}"
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
    exclude_spec_keys: frozenset[str] = frozenset(),
) -> list[ChatTurn]:
    """Строки БД (от старых к новым) -> реплики для провайдера."""
    recent = list(rows)[-max_messages:]
    targets = _spec_targets(recent, exclude_spec_keys)
    turns = [
        t
        for t in (
            row_to_turn(r, frozenset(a for (ri, a) in targets if ri == i))
            for i, r in enumerate(recent)
        )
        if t
    ]
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


def assistant_attachments(rows: Sequence[_Row]) -> list[dict]:
    """Файлы, созданные ассистентом (от старых к новым) — контекст для правок."""
    return [a for r in rows if map_role(r.role) == "assistant" for a in (r.attachments or [])]


async def resolve_edit_target(
    db: AsyncSession, session_id: int, rows: Sequence[_Row], key: str
) -> dict:
    """Файл ассистента с ключом `key` из ЭТОЙ сессии (кнопка «Изменить»). Сначала ищем в
    уже загруженной истории, затем во всей сессии — файл может быть старше окна истории.
    Чужие и несуществующие ключи -> 404, файл без исходных данных -> 400."""
    found = next((a for a in assistant_attachments(rows) if a.get("key") == key), None)
    if found is None:
        result = await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id, ChatMessage.role != "user")
            .where(ChatMessage.attachments.contains([{"key": key}]))
        )
        found = next(
            (a for a in assistant_attachments(result.scalars().all()) if a.get("key") == key), None
        )
    if found is None:
        raise HTTPException(status_code=404, detail="Файл для правки не найден в этой переписке")
    if spec_info(found) is None:
        raise HTTPException(status_code=400, detail="У этого файла нет данных для правки")
    return found


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
