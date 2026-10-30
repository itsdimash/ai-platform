"""Правки созданных файлов: исходные данные (spec) сохраняются, подмешиваются в историю
для модели, версии нумеруются, а клиенту spec не отдаётся."""

from types import SimpleNamespace

import pytest

from app import tools
from app.adapters.base import GenerationResult
from app.services.history import MAX_SPEC_CHARS, SPEC_PER_KIND, assistant_attachments, build_turns
from app.system_prompt import build_system_prompt
from app.tools import ToolContext, apply_tool_calls
from app.utils import attachments as att_mod

PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.fixture(autouse=True)
def stored(monkeypatch):
    async def fake_put(key, data, content_type, *, name=None):
        return None

    async def fake_image(prompt, size=None, quality=None):
        return b"\x89PNG", "img-model"

    monkeypatch.setattr(tools, "put_bytes_async", fake_put)
    monkeypatch.setattr(tools, "generate_image_bytes", fake_image)


def _call(name, args, history=()):
    result = GenerationResult(
        text="x", tokens_in=1, tokens_out=1, latency_ms=1, tool_calls=[{"name": name, "args": args}]
    )
    ctx = ToolContext(user_id=7, session_id=42, history_attachments=list(history))
    return apply_tool_calls(result, ctx=ctx, prompt="p")


def _deck(title="План", **extra):
    return {
        "title": title,
        "theme": "graphite",
        "summary": "Обзор.",
        "slides": [{"layout": "bullets", "title": "A", "bullets": ["x"]}] * 3,
        **extra,
    }


async def test_new_presentation_stores_spec_and_is_v1():
    res = await _call("generate_presentation", _deck())
    att = res.attachments[0]
    assert att["name"] == "План.pptx" and att["revision"] == 1
    assert att["spec"]["title"] == "План" and len(att["spec"]["slides"]) == 3
    assert res.text.startswith("📊 Готово!")


async def test_presentation_edit_is_versioned_and_spec_has_no_revises():
    first = (await _call("generate_presentation", _deck())).attachments[0]
    second = await _call("generate_presentation", _deck(revises="План.pptx"), [first])
    att = second.attachments[0]
    assert att["name"] == "План (v2).pptx" and att["revision"] == 2
    assert "revises" not in att["spec"]
    assert second.text.startswith("📊 Обновлено! Презентация «План (v2)»")

    # модель скопировала суффикс из имени файла — он не должен удваиваться
    third = await _call(
        "generate_presentation", _deck(title="План (v2)", revises="План (v2).pptx"), [first, att]
    )
    assert third.attachments[0]["name"] == "План (v3).pptx"


async def test_unknown_revises_name_falls_back_to_latest_file_of_same_kind():
    deck = (await _call("generate_presentation", _deck())).attachments[0]
    image = (await _call("generate_image", {"prompt": "cat", "caption": "Кот"})).attachments[0]
    res = await _call("generate_presentation", _deck(revises="не то имя.pptx"), [deck, image])
    assert res.attachments[0]["revision"] == 2  # а не правка картинки


async def test_revises_without_history_is_just_a_new_file():
    res = await _call("generate_presentation", _deck(revises="План.pptx"))
    assert res.attachments[0]["name"] == "План.pptx"
    assert res.attachments[0]["revision"] == 1


async def test_image_spec_and_edit():
    first = (
        await _call("generate_image", {"prompt": "a cat", "size": "1024x1024", "caption": "Кот"})
    ).attachments[0]
    assert first["spec"] == {"prompt": "a cat", "size": "1024x1024", "caption": "Кот"}
    assert first["name"] == "Кот.png" and first["type"] == "image"

    res = await _call(
        "generate_image",
        {"prompt": "a cat in a hat", "caption": "Кот в шляпе", "revises": "Кот.png"},
        [first],
    )
    att = res.attachments[0]
    assert att["name"] == "Кот в шляпе (v2).png" and att["revision"] == 2
    assert att["spec"]["prompt"] == "a cat in a hat"
    assert res.text == "🎨 Обновлено: Кот в шляпе"


# --- история -----------------------------------------------------------------------------


def _ai(name, spec=None, mime=PPTX, type_="file"):
    att = {"type": type_, "name": name, "key": f"ai/7/42/{name}", "mime": mime, "size": 1}
    if spec is not None:
        att["spec"] = spec
    return SimpleNamespace(role="ai", content="Готово", attachments=[att])


def _user(text):
    return SimpleNamespace(role="user", content=text, attachments=[])


def test_latest_spec_is_shown_to_the_model_for_edits():
    rows = [
        _user("презентация"),
        _ai("План.pptx", {"title": "План"}),
        _user("добавь слайд"),
    ]
    turns = build_turns(rows)
    assistant = next(t for t in turns if t.role == "assistant")
    assert "[создан файл: План.pptx]" in assistant.content
    assert "Исходные данные файла «План.pptx»" in assistant.content
    assert "generate_presentation" in assistant.content
    assert '"title":"План"' in assistant.content


def test_only_the_latest_specs_per_kind_are_included():
    rows = []
    for i in range(SPEC_PER_KIND + 1):
        rows += [_user(f"п{i}"), _ai(f"Deck{i}.pptx", {"title": f"D{i}"})]
    rows += [_user("картинка"), _ai("c.png", {"prompt": "cat"}, mime="image/png", type_="image")]
    text = "\n".join(t.content for t in build_turns(rows))
    assert text.count("Исходные данные файла") == SPEC_PER_KIND + 1  # 2 презентации + 1 картинка
    assert "Исходные данные файла «Deck0.pptx»" not in text
    assert "Исходные данные файла «Deck2.pptx»" in text
    assert "Исходные данные файла «c.png» для правок (параметры generate_image" in text


def test_oversized_and_legacy_files_get_no_spec():
    big = {"blob": "я" * (MAX_SPEC_CHARS + 10)}
    rows = [_user("a"), _ai("Big.pptx", big), _user("b"), _ai("Old.pptx"), _user("c")]
    text = "\n".join(t.content for t in build_turns(rows))
    assert "Исходные данные" not in text
    assert "[создан файл: Big.pptx]" in text and "[создан файл: Old.pptx]" in text


def test_assistant_attachments_helper_skips_user_rows():
    user = SimpleNamespace(role="user", content="x", attachments=[{"name": "mine.docx"}])
    rows = [user, _ai("План.pptx", {"t": 1})]
    assert [a["name"] for a in assistant_attachments(rows)] == ["План.pptx"]


# --- API и промпт ---------------------------------------------------------------------------


def test_spec_is_not_sent_to_the_client(monkeypatch):
    monkeypatch.setattr(att_mod, "presign_get", lambda key, **kw: f"https://s/{key}")
    rec = {"type": "file", "name": "a.pptx", "key": "ai/7/1/a", "mime": PPTX, "size": 1}
    out = att_mod.with_urls([{**rec, "spec": {"big": 1}, "revision": 2}])[0]
    assert "spec" not in out and out["revision"] == 2 and out["url"] == "https://s/ai/7/1/a"
    assert out["editable"] is True  # UI показывает «Изменить» только для таких файлов
    assert "editable" not in att_mod.with_urls([rec])[0]  # старый файл без исходных данных


def test_system_prompt_explains_edits():
    from datetime import date

    prompt = build_system_prompt("admin", date(2026, 10, 30))
    assert "ПРАВКА" in prompt and "revises" in prompt


# --- Word / PDF / Excel ---------------------------------------------------------------------

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

DOC_ARGS = {
    "title": "Служебная записка",
    "summary": "О закупке.",
    "sections": [{"heading": "Цель", "paragraphs": ["Закупить болты."]}],
}
SHEET_ARGS = {
    "filename": "Бюджет",
    "summary": "Бюджет.",
    "sheets": [{"name": "Q1", "headers": ["Статья", "Сумма"], "rows": [["Аренда", "100"]]}],
}


@pytest.mark.parametrize(
    ("tool", "args", "ext", "key"),
    [
        ("generate_document", DOC_ARGS, ".docx", "title"),
        ("generate_pdf", DOC_ARGS, ".pdf", "title"),
        ("generate_spreadsheet", SHEET_ARGS, ".xlsx", "filename"),
    ],
)
async def test_documents_pdfs_and_spreadsheets_keep_spec_and_support_edits(tool, args, ext, key):
    first = (await _call(tool, dict(args))).attachments[0]
    assert first["revision"] == 1 and first["spec"][key] == args[key]
    assert first["name"] == f"{args[key]}{ext}"

    edited = await _call(
        tool, {**args, "summary": "Добавлена строка.", "revises": first["name"]}, [first]
    )
    att = edited.attachments[0]
    assert att["name"] == f"{args[key]} (v2){ext}" and att["revision"] == 2
    assert "revises" not in att["spec"]
    assert " Обновлено! " in edited.text and "(v2)" in edited.text


async def test_explicit_edit_target_wins_over_revises_and_can_be_outside_history():
    old = (await _call("generate_presentation", _deck())).attachments[0]
    # история пустая (файл старше окна), модель не указала revises — правка всё равно привязана
    res = await apply_tool_calls(
        GenerationResult(
            text="x",
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
            tool_calls=[{"name": "generate_presentation", "args": _deck()}],
        ),
        ctx=ToolContext(user_id=7, session_id=42, edit_target={**old, "revision": 4}),
        prompt="p",
    )
    assert res.attachments[0]["name"] == "План (v5).pptx" and res.attachments[0]["revision"] == 5


async def test_edit_target_of_another_kind_does_not_hijack_a_different_tool():
    deck = (await _call("generate_presentation", _deck())).attachments[0]
    res = await apply_tool_calls(
        GenerationResult(
            text="x",
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
            tool_calls=[{"name": "generate_document", "args": dict(DOC_ARGS)}],
        ),
        ctx=ToolContext(user_id=7, session_id=42, edit_target=deck),
        prompt="p",
    )
    assert res.attachments[0]["name"] == "Служебная записка.docx"  # новый файл, не правка


# --- история: все виды файлов, edit_block, поиск цели --------------------------------------


def test_spec_is_shown_for_documents_and_spreadsheets_too():
    rows = [
        _user("записка"),
        _ai("Записка.docx", {"title": "З"}, mime=DOCX),
        _user("таблица"),
        _ai("Бюджет.xlsx", {"filename": "Б"}, mime=XLSX),
        _user("правка"),
    ]
    text = "\n".join(t.content for t in build_turns(rows))
    assert "для правок (параметры generate_document" in text
    assert "для правок (параметры generate_spreadsheet" in text


def test_excluded_key_is_not_duplicated_in_history():
    ai = _ai("План.pptx", {"title": "План"})
    key = ai.attachments[0]["key"]
    text = "\n".join(
        t.content
        for t in build_turns([_user("a"), ai, _user("b")], exclude_spec_keys=frozenset({key}))
    )
    assert "Исходные данные" not in text


def test_edit_block_contains_spec_tool_and_file_name():
    from app.services.history import edit_block

    att = {**_ai("План.pptx", {"title": "План"}).attachments[0], "revision": 2}
    block = edit_block(att)
    assert "«План.pptx»" in block and "v2" in block
    assert "generate_presentation" in block and '"title":"План"' in block
    assert "revises" in block


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, stmt):
        return _FakeResult(self.rows)


async def test_resolve_edit_target_finds_in_history_then_in_db_and_rejects_others():
    from fastapi import HTTPException

    from app.services.history import resolve_edit_target

    recent = _ai("Новый.pptx", {"title": "Н"})
    older = _ai("Старый.pptx", {"title": "С"})
    legacy = _ai("Legacy.pptx")  # без исходных данных
    db = _FakeDB([older, legacy])

    got = await resolve_edit_target(db, 1, [recent], recent.attachments[0]["key"])
    assert got["name"] == "Новый.pptx"
    got = await resolve_edit_target(db, 1, [recent], older.attachments[0]["key"])  # вне окна
    assert got["name"] == "Старый.pptx"

    with pytest.raises(HTTPException) as nf:
        await resolve_edit_target(db, 1, [recent], "ai/7/1/чужой")
    assert nf.value.status_code == 404
    with pytest.raises(HTTPException) as no_spec:
        await resolve_edit_target(db, 1, [recent], legacy.attachments[0]["key"])
    assert no_spec.value.status_code == 400


# --- промпт по категориям ---------------------------------------------------------------------


def test_prompt_sections_follow_the_task_category():
    from datetime import date

    def prompt(task, **kw):
        return build_system_prompt("admin", date(2026, 10, 30), task, **kw)

    qa = prompt("general_qa")
    assert "Презентации (generate_presentation)" not in qa and "Правки созданных файлов" not in qa
    assert "СРАЗУ создай" in qa  # базовые правила остались
    assert len(qa) < len(prompt(None)) / 3

    pres = prompt("presentation")
    assert "Презентации (generate_presentation)" in pres and "Правки созданных файлов" in pres
    assert "Таблицы:" not in pres and "Изображения:" not in pres
    assert "Таблицы:" in prompt("spreadsheet") and "Изображения:" in prompt("image")
    assert "Документы и PDF:" in prompt("pdf") and "Документы и PDF:" in prompt(
        "contract_generation"
    )

    # классификатор не уверен -> модель может создать любой файл: все блоки
    for text in (prompt("general_qa", broad=True), prompt(None)):
        assert all(
            m in text for m in ("Презентации (generate_presentation)", "Таблицы:", "Изображения:")
        )
