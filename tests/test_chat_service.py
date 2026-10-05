import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import tools
from app.adapters.base import FINISH_MAX_TOKENS, FINISH_REFUSAL, ModelCaps
from app.auth import CurrentUser, get_current_user
from app.classifier.classify import Classification
from app.db.session import get_db
from app.errors import GenerationFailed, ModelRefusal, OutputTruncated
from app.main import app
from app.models.chat import ChatMessage
from app.models.logs import AIRequestLog
from app.router.route import RouteDecision, load_config
from app.routers.deps import get_adapters
from app.services import chat_service
from app.services.chat_service import fail_request, failure_for, generate_with_tools
from app.system_prompt import hard_tool_instruction, tool_hint
from app.tools import ToolExecutionError
from tests.helpers import FakeAdapter, FakeDB, db_row, fake_adapters, result

TOOL = "generate_presentation"
PRES_ARGS = {
    "title": "Коты",
    "slides": [{"title": "Введение", "content": ["a", "b"]}] * 11,
    "summary": "Обзор пород и привычек.",
}


def decision(forced=TOOL, thinking="medium", model="claude-sonnet", web_search=False):
    return RouteDecision(
        model=model,
        web_search=web_search,
        require_human_review=False,
        used_fallback_confidence=False,
        max_tokens=32000,
        thinking=thinking,
        forced_tool=forced,
        timeout_s=600,
    )


def with_caps(name, **overrides):
    adapter = FakeAdapter(name)
    adapter.caps = ModelCaps(**{**adapter.caps.__dict__, **overrides})
    return adapter


async def run(adapter, dec=None, script=None):
    adapter.script = list(script or [])
    return await generate_with_tools(
        adapter, turns=[], system="SYS", decision=dec or decision(), attachments=None
    )


# --- конечный автомат принудительного tool --------------------------------------------------


async def test_models_that_accept_tool_choice_are_forced_immediately():
    adapter = FakeAdapter("gemini-pro")  # forced_tool: with_thinking
    _, fallback = await run(adapter, script=[result(TOOL, PRES_ARGS)])
    assert fallback is False and len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call["force_tool"] == TOOL and call["thinking"] == "medium" and call["system"] == "SYS"
    assert call["max_tokens"] == 32000 and call["timeout_s"] == 600


async def test_claude_55_cannot_be_forced_so_attempt_one_is_auto_with_hint():
    adapter = FakeAdapter("claude-sonnet")  # forced_tool: unsupported
    _, fallback = await run(adapter, script=[result(TOOL, PRES_ARGS)])
    assert fallback is False and len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call["force_tool"] is None and call["thinking"] == "medium"
    assert call["system"] == "SYS" + tool_hint(TOOL)


async def test_claude_55_second_attempt_uses_hard_instruction_and_sets_forced_fallback():
    adapter = FakeAdapter("claude-sonnet")
    res, fallback = await run(
        adapter,
        script=[
            result(text="вот план", tokens_in=100, tokens_out=50),
            result(TOOL, PRES_ARGS, tokens_in=7, tokens_out=3),
        ],
    )
    assert fallback is True and len(adapter.calls) == 2
    second = adapter.calls[1]
    assert second.get("force_tool") is None and second["thinking"] == "low"
    assert second["system"] == "SYS" + tool_hint(TOOL) + hard_tool_instruction(TOOL)
    assert (res.tokens_in, res.tokens_out) == (107, 53)  # токены обеих попыток суммируются


async def test_claude_55_never_calls_tool_raises_generation_failed():
    adapter = FakeAdapter("claude-sonnet")
    with pytest.raises(GenerationFailed):
        await run(adapter, script=[result(text="a"), result(text="b")])
    assert len(adapter.calls) == 2  # ровно две попытки


async def test_claude_sonnet_5_style_forced_with_thinking_then_forced_without():
    adapter = with_caps("claude-sonnet", forced_tool="with_thinking")
    _, fallback = await run(adapter, script=[result(text="нет"), result(TOOL, PRES_ARGS)])
    first, second = adapter.calls
    assert first["force_tool"] == TOOL and first["thinking"] == "medium"
    assert second["force_tool"] == TOOL and second["thinking"] == "off"
    assert fallback is True


async def test_haiku_forced_tool_requires_thinking_off():
    adapter = FakeAdapter("claude-haiku")  # forced_tool: no_thinking
    await run(
        adapter,
        dec=decision(model="claude-haiku", thinking="medium"),
        script=[result(TOOL, PRES_ARGS)],
    )
    assert adapter.calls[0]["force_tool"] == TOOL and adapter.calls[0]["thinking"] == "off"


async def test_any_file_tool_counts_as_success():
    adapter = FakeAdapter("gemini-flash")
    _, fallback = await run(adapter, script=[result("generate_pdf", {"title": "x"})])
    assert fallback is False


async def test_refusal_raises_model_refusal_without_retry():
    adapter = FakeAdapter("claude-sonnet")
    with pytest.raises(ModelRefusal):
        await run(adapter, script=[result(finish=FINISH_REFUSAL, detail="cyber"), result(TOOL)])
    assert len(adapter.calls) == 1


async def test_truncation_with_expected_tool_raises_output_truncated_without_retry():
    adapter = FakeAdapter("claude-sonnet")
    with pytest.raises(OutputTruncated):
        await run(adapter, script=[result(TOOL, PRES_ARGS, finish=FINISH_MAX_TOKENS)])
    assert len(adapter.calls) == 1


async def test_truncation_on_second_attempt_and_refusal_on_second_attempt():
    adapter = FakeAdapter("claude-sonnet")
    with pytest.raises(OutputTruncated):
        await run(adapter, script=[result(text="x"), result(finish=FINISH_MAX_TOKENS)])
    adapter = FakeAdapter("claude-sonnet")
    with pytest.raises(ModelRefusal):
        await run(adapter, script=[result(text="x"), result(finish=FINISH_REFUSAL)])


async def test_plain_answer_without_forced_tool():
    adapter = FakeAdapter("claude-sonnet")
    _, fallback = await run(
        adapter,
        dec=decision(forced=None, thinking="low"),
        script=[result(text="Валовая маржа — это...")],
    )
    assert fallback is False and len(adapter.calls) == 1
    assert adapter.calls[0].get("force_tool") is None and adapter.calls[0]["system"] == "SYS"
    assert "tools" not in adapter.calls[0]  # по умолчанию — все файловые tools в режиме auto


async def test_plain_answer_truncated_text_is_returned_but_truncated_tool_call_raises():
    adapter = FakeAdapter("claude-sonnet")
    res, _ = await run(
        adapter,
        dec=decision(forced=None),
        script=[result(text="длинный...", finish=FINISH_MAX_TOKENS)],
    )
    assert res.text == "длинный..."
    with pytest.raises(OutputTruncated):
        await run(
            FakeAdapter("claude-sonnet"),
            dec=decision(forced=None),
            script=[result(TOOL, PRES_ARGS, finish=FINISH_MAX_TOKENS)],
        )


async def test_web_search_requests_pass_no_file_tools():
    adapter = FakeAdapter("claude-sonnet")
    await run(adapter, dec=decision(forced=None, web_search=True), script=[result(text="курс 450")])
    assert adapter.calls[0]["tools"] == [] and adapter.calls[0]["web_search"] is True


# --- коды ошибок -------------------------------------------------------------------------------


def test_failure_mapping_codes_and_messages():
    expected = {
        ModelRefusal: "model_refusal",
        OutputTruncated: "output_truncated",
        GenerationFailed: "generation_failed",
    }
    for exc_type, code in expected.items():
        status, detail, log = failure_for(exc_type("внутренняя причина"))
        assert status == 502
        assert detail["code"] == code and set(detail) == {"code", "message"}
        assert "внутренняя" not in detail["message"] and detail["message"]
        assert log.startswith(code) and "внутренняя причина" in log
    # остальные ошибки — как были
    status, detail, _ = failure_for(ToolExecutionError("generate_pdf", RuntimeError("boom")))
    assert status == 500 and detail == "Не удалось сформировать файл. Попробуйте ещё раз."
    status, detail, log = failure_for(RuntimeError("secret provider text"))
    assert status == 502 and isinstance(detail, str) and "secret" not in detail and "secret" in log
    status, detail, _ = failure_for(HTTPException(400, detail="плохой файл"))
    assert (status, detail) == (400, "плохой файл")


async def test_fail_request_returns_json_detail_object_and_rolls_back_new_session():
    db = FakeDB()
    with pytest.raises(HTTPException) as e:
        await fail_request(
            db,
            user_id=7,
            session_id=99,
            session_created=True,
            task_type="presentation",
            confidence=0.9,
            used_fallback_confidence=False,
            model_used="claude-sonnet-5-5",
            started=0.0,
            status_code=502,
            detail={"code": "model_refusal", "message": "Модель отклонила запрос"},
            error_message="model_refusal: cyber",
            classifier_tokens_in=5,
            forced_fallback=True,
        )
    assert e.value.status_code == 502 and e.value.detail == {
        "code": "model_refusal",
        "message": "Модель отклонила запрос",
    }
    db.rollback.assert_awaited_once()
    log = db.of(AIRequestLog)[0]
    assert log.session_id is None and log.status == "error" and log.forced_fallback is True
    assert log.classifier_tokens_in == 5 and log.model_used == "claude-sonnet-5-5"


# --- ход чата через HTTP ---------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    state = {
        "adapters": fake_adapters(),
        "classification": Classification("general_qa", 0.9, "", 100, 10),
    }
    stored: list[dict] = []

    async def fake_classify(prompt, adapter, **kwargs):
        state["classify_args"] = {"prompt": prompt, **kwargs}
        return state["classification"]

    async def fake_put(key, data, content_type, *, name=None):
        stored.append({"key": key, "mime": content_type})

    async def fake_image(prompt, size=None, quality=None):
        state["image_prompt"] = prompt
        return b"\x89PNG\r\n\x1a\nfake", "gpt-image-2.5-flare"

    monkeypatch.setattr(chat_service, "classify", fake_classify)
    monkeypatch.setattr(tools, "put_bytes_async", fake_put)
    monkeypatch.setattr(tools, "generate_image_bytes", fake_image)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_adapters] = lambda: state["adapters"]
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id=7, role="pm")

    def post(prompt="Создай презентацию на тему: cats", **extra):
        return TestClient(app).post("/v1/chat", json={"prompt": prompt, **extra})

    yield type("Env", (), {"db": db, "state": state, "post": staticmethod(post), "stored": stored})
    app.dependency_overrides.clear()


def classify_as(env, task, confidence=0.95, tin=100, tout=10):
    env.state["classification"] = Classification(task, confidence, "тест", tin, tout)


def use(env, **scripts):
    env.state["adapters"] = fake_adapters(**scripts)
    return env.state["adapters"]


def test_presentation_request_routes_to_sonnet_and_returns_file(env):
    classify_as(env, "presentation")
    adapters = use(env, claude_sonnet=[result(TOOL, PRES_ARGS, model_used="claude-sonnet-5-5")])
    r = env.post()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model_used"] == "claude-sonnet-5-5" and body["task_type"] == "presentation"
    assert body["text"] == "📊 Готово! Презентация «Коты» — 12 слайдов. Обзор пород и привычек."
    assert len(body["attachments"]) == 1 and body["attachments"][0]["name"] == "Коты.pptx"
    assert body["image_model_used"] is None and body["needs_review"] is False
    assert (
        adapters["claude-sonnet"].calls[0]["messages"][-1].content
        == "Создай презентацию на тему: cats"
    )
    assert adapters["gemini-flash"].calls == []  # flash в этом пути не участвует
    log = env.db.of(AIRequestLog)[0]
    assert (
        log.forced_fallback is False
        and log.classifier_tokens_in == 100
        and log.classifier_tokens_out == 10
    )
    assert log.image_model is None and log.status == "success"


def test_image_request_reports_text_model_and_image_model_separately(env):
    classify_as(env, "image")
    use(
        env,
        claude_sonnet=[
            result(
                "generate_image",
                {"prompt": "A cat with a toy", "caption": "Кот играет с игрушкой"},
                model_used="claude-sonnet-5-5",
            )
        ],
    )
    r = env.post("Сгенерируй изображение: the cat with toy")
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["text"] == "🎨 Готово: Кот играет с игрушкой"
    assert (
        body["model_used"] == "claude-sonnet-5-5"
        and body["image_model_used"] == "gpt-image-2.5-flare"
    )
    assert body["attachments"][0]["type"] == "image"
    assert env.db.of(AIRequestLog)[0].image_model == "gpt-image-2.5-flare"
    ai_msg = next(m for m in env.db.of(ChatMessage) if m.role == "ai")
    assert ai_msg.image_model == "gpt-image-2.5-flare" and ai_msg.model_used == "claude-sonnet-5-5"
    user_msg = next(m for m in env.db.of(ChatMessage) if m.role == "user")
    assert user_msg.image_model is None


def test_forced_fallback_is_logged(env):
    classify_as(env, "presentation")
    use(env, claude_sonnet=[result(text="Вот план презентации..."), result(TOOL, PRES_ARGS)])
    r = env.post()
    assert r.status_code == 200
    assert env.db.of(AIRequestLog)[0].forced_fallback is True


def test_fallback_model_is_recorded_as_actual_model_and_marked_in_error_message(env):
    classify_as(env, "presentation")
    use(env, claude_sonnet=[result(TOOL, PRES_ARGS, model_used="claude-opus-5-5")])
    r = env.post()
    assert r.json()["model_used"] == "claude-opus-5-5"
    log = env.db.of(AIRequestLog)[0]
    assert log.model_used == "claude-opus-5-5" and log.status == "success"
    assert "fallback: claude-sonnet-5-5 -> claude-opus-5-5" in log.error_message
    assert next(m for m in env.db.of(ChatMessage) if m.role == "ai").model_used == "claude-opus-5-5"


def test_dated_snapshot_is_not_reported_as_fallback(env):
    classify_as(env, "translation")
    use(env, claude_haiku=[result(text="Hello", model_used="claude-haiku-4-5-20251001")])
    r = env.post("Переведи: Привет")
    assert r.status_code == 200
    assert env.db.of(AIRequestLog)[0].error_message is None


@pytest.mark.parametrize(
    ("script", "code"),
    [
        ([result(finish=FINISH_REFUSAL, detail="cyber")], "model_refusal"),
        ([result(TOOL, PRES_ARGS, finish=FINISH_MAX_TOKENS)], "output_truncated"),
        ([result(text="а"), result(text="б")], "generation_failed"),
    ],
)
def test_three_error_codes_over_http_and_nothing_is_saved(env, script, code):
    classify_as(env, "presentation")
    use(env, claude_sonnet=script)
    r = env.post()
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["code"] == code and set(detail) == {"code", "message"}
    assert detail["message"] and "claude" not in detail["message"].lower()
    assert env.db.of(ChatMessage) == []  # ни ответ, ни сообщение пользователя не сохранены
    log = env.db.of(AIRequestLog)[0]
    assert log.status == "error" and log.error_message.startswith(code) and log.session_id is None
    env.db.rollback.assert_awaited()  # новая сессия откатывается


def test_other_errors_stay_as_they_were(env):
    classify_as(env, "general_qa")
    use(env, claude_sonnet=[RuntimeError("provider exploded: key sk-...")])
    r = env.post("что такое маржа?")
    assert r.status_code == 502 and isinstance(r.json()["detail"], str)
    assert (
        "exploded" not in r.json()["detail"]
        and "RuntimeError" in env.db.of(AIRequestLog)[0].error_message
    )


def test_low_confidence_goes_to_strong_model_without_forcing(env):
    classify_as(env, "presentation", confidence=0.4)
    adapters = use(env, claude_sonnet=[result(text="Уточните тему?")])
    r = env.post()
    assert r.status_code == 200 and r.json()["model_used"] == "claude-sonnet-5-5"
    call = adapters["claude-sonnet"].calls[0]
    assert call.get("force_tool") is None and call["thinking"] == "low"
    assert adapters["gemini-flash"].calls == [] and adapters["gemini-flash-lite"].calls == []
    assert env.db.of(AIRequestLog)[0].used_fallback_confidence is True


def test_explicit_model_is_respected_and_category_still_applies(env):
    classify_as(env, "presentation")
    adapters = use(env, claude_opus=[result(TOOL, PRES_ARGS)])
    r = env.post(model="claude-opus")
    assert r.status_code == 200 and r.json()["model_used"] == "claude-opus-5-5"
    assert (
        adapters["claude-opus"].calls[0]["system"].endswith(tool_hint(TOOL))
    )  # tool закреплён подсказкой
    assert adapters["claude-sonnet"].calls == []


def test_explicit_gemini_gets_forced_tool_choice(env):
    classify_as(env, "presentation")
    adapters = use(env, gemini_flash=[result(TOOL, PRES_ARGS)])
    assert env.post(model="gemini-flash").status_code == 200
    assert adapters["gemini-flash"].calls[0]["force_tool"] == TOOL


def test_fable_is_used_only_on_explicit_choice(env):
    classify_as(env, "general_qa")
    adapters = use(env)
    env.post("привет")
    assert adapters["claude-fable"].calls == []
    env.post("привет", model="claude-fable")
    assert len(adapters["claude-fable"].calls) == 1


def test_unknown_explicit_model_is_400_and_logged(env):
    r = env.post(model="gpt-9")
    assert r.status_code == 400 and "gpt-9" in r.json()["detail"]
    assert env.state["adapters"]["gemini-flash-lite"].calls == []
    assert env.db.of(AIRequestLog)[0].status == "error"


def test_history_is_sent_as_role_messages_with_attachment_notes(env):
    env.db.history_rows = [
        db_row("user", "сделай презентацию про котов"),
        db_row("ai", "📊 Готово!", [{"type": "file", "name": "Коты.pptx"}]),
    ]
    classify_as(env, "presentation")
    adapters = use(env, claude_sonnet=[result(TOOL, PRES_ARGS)])
    env.post("добавь слайд про цены")
    turns = adapters["claude-sonnet"].calls[0]["messages"]
    assert [(t.role, t.content) for t in turns] == [
        ("user", "сделай презентацию про котов"),
        ("assistant", "📊 Готово!\n[создан файл: Коты.pptx]"),
        ("user", "добавь слайд про цены"),
    ]
    # классификатор получил контекст, а не склеенную строку
    assert [t.role for t in env.state["classify_args"]["context"]] == ["user", "assistant"]
    assert env.state["classify_args"]["prompt"] == "добавь слайд про цены"


def test_db_query_path_uses_haiku_and_sql_helper(env, monkeypatch):
    seen = {}

    async def fake_sql(prompt, user, adapter):
        seen["adapter"] = adapter.model
        return "Найдено строк: 2", [{"Название": "Болт"}], 11, 22

    monkeypatch.setattr(chat_service, "_handle_db_query", fake_sql)
    classify_as(env, "db_query")
    use(env)
    body = env.post("сколько болтов на складе?").json()
    assert seen["adapter"] == "claude-haiku-4-5-20251001" and body["table"] == [
        {"Название": "Болт"}
    ]
    assert body["model_used"] == "claude-haiku-4-5-20251001" and body["attachments"] == []


def test_db_query_with_attachments_is_routed_as_general_qa(env, monkeypatch):
    async def must_not_run(*a, **k):
        raise AssertionError("SQL-путь с вложениями использоваться не должен")

    monkeypatch.setattr(chat_service, "_handle_db_query", must_not_run)
    classify_as(env, "db_query")
    use(env)
    key = "ai/7/_pending/uploads/" + "a" * 32 + "_смета.xlsx"

    async def fake_head(k):
        return {"size": 10, "mime": "application/octet-stream", "name": None}

    from app.utils import attachments

    monkeypatch.setattr(attachments, "head_info_async", fake_head)
    r = env.post("сколько тут строк?", attachment_keys=[key])
    assert r.status_code == 200 and r.json()["model_used"] == "claude-sonnet-5-5"
    assert env.state["classify_args"]["attachment_kinds"] == "attached: 1 file"


def test_response_json_shape_is_additive(env):
    classify_as(env, "general_qa")
    use(env, claude_sonnet=[result(text="ответ", model_used="claude-sonnet-5-5")])
    body = env.post("привет").json()
    assert set(body) == {
        "session_id",
        "text",
        "task_type",
        "model_used",
        "confidence",
        "tokens_in",
        "tokens_out",
        "latency_ms",
        "table",
        "needs_review",
        "attachments",
        "image_model_used",
    }
    assert body["table"] is None and body["attachments"] == [] and body["image_model_used"] is None


def test_today_local_falls_back_to_utc(monkeypatch):
    from datetime import date

    assert isinstance(chat_service.today_local(), date)
    monkeypatch.setattr(chat_service, "load_config", lambda: {"timezone": "Mars/Olympus"})
    assert isinstance(chat_service.today_local(), date)


def test_system_prompt_gets_role_and_date(env):
    classify_as(env, "general_qa")
    adapters = use(env, claude_sonnet=[result(text="ок")])
    env.post("привет")
    system = adapters["claude-sonnet"].calls[0]["system"]
    assert "менеджер проектов" in system  # роль pm из JWT
    assert load_config() and "Сегодня " in system


def test_system_prompt_covers_language_title_slide_and_blocks():
    from datetime import date

    from app.system_prompt import build_system_prompt

    text = build_system_prompt("admin", date(2026, 10, 5))
    assert "05.10.2026" in text and "администратор" in text
    for needle in (
        "на языке запроса пользователя",  # язык по запросу, русский по умолчанию
        "не дублируй",  # титульный слайд создаётся автоматически
        "СРАЗУ создай",  # проактивность
        "Презентации (generate_presentation):",
        "Документы и PDF:",
        "Таблицы:",
        "Изображения:",  # отдельные блоки
        "caption",
        "summary",
    ):
        assert needle in text, needle
    assert "кладовщик" in build_system_prompt("warehouse", date(2026, 1, 1))
    assert "mystery" in build_system_prompt(
        "mystery", date(2026, 1, 1)
    )  # неизвестная роль — как есть
