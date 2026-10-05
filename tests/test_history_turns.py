from types import SimpleNamespace

from app.adapters.base import ChatTurn
from app.services.history import (
    build_turns,
    estimate_tokens,
    glue_context,
    map_role,
    merge_consecutive,
    row_to_turn,
    with_user_message,
)


def row(role, content, attachments=None):
    return SimpleNamespace(role=role, content=content, attachments=attachments or [])


def test_role_mapping_db_ai_becomes_assistant():
    assert map_role("user") == "user"
    assert map_role("ai") == "assistant"
    assert map_role("ai-clarify") == "assistant"
    turns = build_turns([row("user", "q"), row("ai", "a"), row("ai-clarify", "уточнение")])
    assert [t.role for t in turns] == ["user", "assistant"]  # подряд идущие assistant склеены


def test_keeps_only_the_last_40_messages():
    rows = []
    for i in range(60):
        rows.append(row("user", f"u{i}"))
        rows.append(row("ai", f"a{i}"))
    turns = build_turns(rows)
    assert len(turns) == 40
    assert turns[0].content == "u40" and turns[-1].content == "a59"
    assert len(build_turns(rows, max_messages=10)) == 10


def test_leading_assistant_turns_are_dropped():
    turns = build_turns([row("ai", "висит в начале"), row("user", "вопрос"), row("ai", "ответ")])
    assert turns[0].role == "user" and turns[0].content == "вопрос"


def test_attachment_notes_for_assistant_and_user():
    file_msg = row("ai", "📊 Готово!", [{"type": "file", "name": "Отчёт.pptx"}])
    image_msg = row("ai", "🎨 Готово", [{"type": "image", "name": "cat.png"}])
    user_msg = row("user", "смотри", [{"type": "file", "name": "договор.docx"}])
    assert row_to_turn(file_msg).content.endswith("[создан файл: Отчёт.pptx]")
    assert row_to_turn(image_msg).content.endswith("[создано изображение: cat.png]")
    assert row_to_turn(user_msg).content.endswith("[приложено: договор.docx]")


def test_multimodal_user_marker_is_not_duplicated():
    msg = row(
        "user",
        "анализ\n\n[Приложено: 1 PDF. Вложения обработаны...]",
        [{"type": "file", "name": "a.pdf"}],
    )
    assert "[приложено:" not in row_to_turn(msg).content


def test_empty_messages_are_skipped_but_attachment_only_message_survives():
    assert row_to_turn(row("user", "   ")) is None
    only_file = row_to_turn(row("ai", "", [{"type": "file", "name": "x.pdf"}]))
    assert only_file.content == "[создан файл: x.pdf]"


def test_consecutive_same_role_turns_merge():
    merged = merge_consecutive(
        [ChatTurn("user", "a"), ChatTurn("user", "b"), ChatTurn("assistant", "c")]
    )
    assert [(t.role, t.content) for t in merged] == [("user", "a\n\nb"), ("assistant", "c")]


def test_token_trim_drops_oldest_but_keeps_last_and_starts_with_user():
    big = "я" * 2500  # ~1000 токенов по оценке 2,5 символа/токен
    rows = [
        row("user", big),
        row("ai", big),
        row("user", big),
        row("ai", big),
        row("user", "последний"),
    ]
    turns = build_turns(rows, max_tokens=2500)
    total = sum(estimate_tokens(t.content) for t in turns)
    assert total <= 2500 + 5 and turns[-1].content == "последний" and turns[0].role == "user"
    assert len(turns) < 5
    # даже при абсурдно малом бюджете остаётся последняя реплика
    assert build_turns(rows, max_tokens=1)[-1].content == "последний"


def test_estimate_tokens_is_conservative_for_russian():
    assert estimate_tokens("а" * 250) == 101
    assert estimate_tokens("") == 1


def test_with_user_message_appends_or_merges():
    base = [ChatTurn("user", "q"), ChatTurn("assistant", "a")]
    assert [t.role for t in with_user_message(base, "new")] == ["user", "assistant", "user"]
    assert with_user_message([ChatTurn("user", "q")], "new")[-1].content == "q\n\nnew"
    assert with_user_message([], "hi") == [ChatTurn("user", "hi")]


def test_glue_context_for_sql_path_keeps_last_six_only():
    rows = [row("user" if i % 2 == 0 else "ai", f"m{i}") for i in range(10)]
    text = glue_context(rows, "и по складу?")
    assert (
        "m3" not in text
        and "m4" in text
        and text.endswith("Новый вопрос пользователя: и по складу?")
    )
    assert glue_context([], "вопрос") == "вопрос"
