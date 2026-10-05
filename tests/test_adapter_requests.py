"""Формы запросов к провайдерам (принудительный tool, thinking, fallbacks, таймауты).
SDK замоканы — сетевых вызовов нет."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.base import (
    FINISH_MAX_TOKENS,
    FINISH_REFUSAL,
    FINISH_STOP,
    Attachment,
    ChatTurn,
    ModelCaps,
)
from app.adapters.gemini_adapter import resolve_thinking_level
from app.adapters.openai_adapter import OpenAIAdapter, resolve_reasoning_effort
from app.adapters.registry import build_adapters
from app.config import get_settings
from app.tools import ALL_TOOLS

ADAPTERS = build_adapters(get_settings())  # настоящие адаптеры из config.yaml, без сети
TOOL = "generate_presentation"
TURNS = [
    ChatTurn("user", "привет"),
    ChatTurn("assistant", "здравствуйте"),
    ChatTurn("user", "сделай"),
]


# --- Anthropic ----------------------------------------------------------------------------


class FakeStream:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return self.message


def claude_message(content=None, stop_reason="end_turn", model="claude-sonnet-5-5", details=None):
    return SimpleNamespace(
        id="msg_1",
        model=model,
        content=content or [SimpleNamespace(type="text", text="ок")],
        stop_reason=stop_reason,
        stop_details=details,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def patch_claude(adapter, message=None):
    seen = {}

    def make(path):
        def stream(**kwargs):
            seen["path"], seen["kwargs"] = path, kwargs
            return FakeStream(message or claude_message())

        return stream

    adapter.client.messages.stream = make("messages")
    adapter.client.beta.messages.stream = make("beta")
    return seen


async def claude_call(name, **kw):
    adapter = ADAPTERS[name]
    seen = patch_claude(adapter, kw.pop("message", None))
    result = await adapter.generate(messages=TURNS, max_tokens=kw.pop("max_tokens", 32000), **kw)
    return seen, result


async def test_claude_uses_streaming_helper_with_large_max_tokens_and_timeout():
    seen, _ = await claude_call("claude-haiku", timeout_s=123)
    assert seen["path"] == "messages"
    assert seen["kwargs"]["max_tokens"] == 32000 and seen["kwargs"]["timeout"] == 123
    assert seen["kwargs"]["model"] == "claude-haiku-4-5-20251001"
    assert [m["role"] for m in seen["kwargs"]["messages"]] == ["user", "assistant", "user"]


@pytest.mark.parametrize("name", ["claude-sonnet", "claude-opus"])
async def test_server_side_refusal_fallbacks_are_enabled_for_sonnet_and_opus(name):
    seen, _ = await claude_call(name)
    assert seen["path"] == "beta"
    assert seen["kwargs"]["betas"] == ["server-side-fallback-2026-07-01"]
    assert seen["kwargs"]["fallbacks"] == "default"


async def test_no_fallbacks_for_haiku_and_fable():
    for name in ("claude-haiku", "claude-fable"):
        seen, _ = await claude_call(name)
        assert seen["path"] == "messages" and "fallbacks" not in seen["kwargs"]


async def test_forced_tool_choice_shape():
    seen, _ = await claude_call("claude-sonnet", force_tool=TOOL)
    assert seen["kwargs"]["tool_choice"] == {"type": "tool", "name": TOOL}
    seen, _ = await claude_call("claude-sonnet")
    assert "tool_choice" not in seen["kwargs"]


@pytest.mark.parametrize(
    ("name", "thinking", "expected_thinking", "expected_output"),
    [
        ("claude-sonnet", "medium", {"type": "adaptive"}, {"effort": "medium"}),
        ("claude-sonnet", "max", {"type": "adaptive"}, {"effort": "max"}),
        (
            "claude-sonnet",
            "off",
            {"type": "between_tools"},
            {"effort": "low"},
        ),  # disabled -> 400 у 5.5
        ("claude-opus", "off", {"type": "adaptive"}, {"effort": "low"}),  # выключить нельзя
        ("claude-opus", "high", {"type": "adaptive"}, {"effort": "high"}),
        ("claude-fable", "off", {"type": "adaptive"}, {"effort": "low"}),
        ("claude-haiku", "medium", {"type": "enabled", "budget_tokens": 6000}, None),
        ("claude-haiku", "off", None, None),
    ],
)
async def test_claude_thinking_params_per_model(name, thinking, expected_thinking, expected_output):
    seen, _ = await claude_call(name, thinking=thinking)
    assert seen["kwargs"].get("thinking") == expected_thinking
    assert seen["kwargs"].get("output_config") == expected_output


async def test_haiku_forced_tool_drops_thinking_and_small_budgets_are_skipped():
    seen, _ = await claude_call("claude-haiku", force_tool=TOOL, thinking="high")
    assert seen["kwargs"]["tool_choice"]["name"] == TOOL and "thinking" not in seen["kwargs"]
    seen, _ = await claude_call("claude-haiku", thinking="high", max_tokens=1500)  # бюджет < 1024
    assert "thinking" not in seen["kwargs"]


async def test_claude_tools_and_web_search_params():
    seen, _ = await claude_call("claude-sonnet", tools=[])
    assert "tools" not in seen["kwargs"]
    seen, _ = await claude_call("claude-sonnet")
    assert {t["name"] for t in seen["kwargs"]["tools"]} == {t["name"] for t in ALL_TOOLS}
    seen, _ = await claude_call("claude-sonnet", web_search=True, tools=[])
    ws = seen["kwargs"]["tools"][0]
    assert ws["type"] == "web_search_20250305" and ws["name"] == "web_search"


async def test_claude_response_parsing_tool_calls_model_and_finish():
    msg = claude_message(
        [SimpleNamespace(type="tool_use", name=TOOL, input={"title": "t"})],
        stop_reason="tool_use",
        model="claude-opus-5-5",
    )
    _, result = await claude_call("claude-sonnet", message=msg)
    assert result.tool_calls == [{"name": TOOL, "args": {"title": "t"}}]
    assert result.model_used == "claude-opus-5-5"  # фактическая модель из ответа (fallback виден)
    assert result.finish == FINISH_STOP and (result.tokens_in, result.tokens_out) == (10, 5)


async def test_claude_refusal_and_truncation_are_normalized():
    refusal = claude_message(stop_reason="refusal", details=SimpleNamespace(category="cyber"))
    _, result = await claude_call("claude-sonnet", message=refusal)
    assert result.finish == FINISH_REFUSAL and result.finish_detail == "cyber"
    _, result = await claude_call("claude-sonnet", message=claude_message(stop_reason="max_tokens"))
    assert result.finish == FINISH_MAX_TOKENS


async def test_claude_attachments_go_to_last_user_turn():
    adapter = ADAPTERS["claude-sonnet"]
    seen = patch_claude(adapter)
    await adapter.generate(
        messages=TURNS,
        attachments=[
            Attachment("image/png", b"x", "image"),
            Attachment("application/pdf", b"y", "pdf_document"),
        ],
    )
    msgs = seen["kwargs"]["messages"]
    assert isinstance(msgs[0]["content"], str) and isinstance(msgs[-1]["content"], list)
    assert [b["type"] for b in msgs[-1]["content"]] == ["text", "image", "document"]


async def test_old_prompt_path_still_works():
    adapter = ADAPTERS["claude-haiku"]
    seen = patch_claude(adapter)
    await adapter.generate("просто строка", max_tokens=100)
    assert seen["kwargs"]["messages"] == [{"role": "user", "content": "просто строка"}]


def test_claude_default_timeout_and_retries_come_from_config():
    client = ADAPTERS["claude-sonnet"].client
    assert client.timeout == 600 and client.max_retries == 2


# --- Gemini ---------------------------------------------------------------------------------


def gemini_response(text="ок", calls=None, finish="STOP", block=None, thoughts=7):
    cand = SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(text=text, thought=False)]),
        finish_reason=SimpleNamespace(name=finish),
    )
    return SimpleNamespace(
        candidates=[cand],
        function_calls=calls or [],
        prompt_feedback=SimpleNamespace(block_reason=block) if block else None,
        usage_metadata=SimpleNamespace(
            prompt_token_count=11, candidates_token_count=4, thoughts_token_count=thoughts
        ),
        model_version="gemini-3.8-flash",
    )


async def gemini_call(name="gemini-flash", response=None, **kw):
    adapter = ADAPTERS[name]
    adapter.client.aio.models.generate_content = AsyncMock(
        return_value=response or gemini_response()
    )
    result = await adapter.generate(messages=TURNS, max_tokens=32000, **kw)
    return adapter.client.aio.models.generate_content.await_args.kwargs, result


async def test_gemini_forced_tool_uses_any_mode_with_allowed_names():
    kwargs, _ = await gemini_call(force_tool=TOOL, thinking="medium")
    fc = kwargs["config"].tool_config.function_calling_config
    assert fc.mode.value == "ANY" and fc.allowed_function_names == [TOOL]
    assert kwargs["config"].thinking_config.thinking_level.value == "MEDIUM"
    kwargs, _ = await gemini_call()
    assert kwargs["config"].tool_config is None


@pytest.mark.parametrize(
    ("name", "thinking", "expected"),
    [
        ("gemini-flash", "off", "low"),  # 3.8-flash не принимает minimal
        ("gemini-flash", "low", "low"),
        ("gemini-flash", "max", "high"),
        ("gemini-pro", "off", "low"),
        ("gemini-flash-lite", "off", "minimal"),
        ("gemini-flash-lite", "medium", "medium"),
    ],
)
async def test_gemini_thinking_level_is_clamped_to_model_support(name, thinking, expected):
    kwargs, _ = await gemini_call(name, thinking=thinking)
    assert kwargs["config"].thinking_config.thinking_level.value == expected.upper()


def test_resolve_thinking_level_nearest_supported():
    assert resolve_thinking_level("off", ("low", "medium", "high")) == "low"
    assert resolve_thinking_level("high", ("low",)) == "low"
    assert resolve_thinking_level("medium", ()) is None


async def test_gemini_web_search_drops_function_tools_and_forcing():
    kwargs, _ = await gemini_call(web_search=True, force_tool=TOOL)
    tools = kwargs["config"].tools
    assert (
        len(tools) == 1
        and tools[0].google_search is not None
        and not tools[0].function_declarations
    )
    assert kwargs["config"].tool_config is None


async def test_gemini_json_mode_has_no_tools():
    kwargs, _ = await gemini_call("gemini-flash-lite", json_mode=True, tools=[])
    assert kwargs["config"].response_mime_type == "application/json" and not kwargs["config"].tools


async def test_gemini_timeout_and_retry_options():
    kwargs, _ = await gemini_call(timeout_s=77)
    opts = kwargs["config"].http_options
    assert opts.timeout == 77_000 and opts.retry_options.attempts == 3  # 1 + 2 ретрая
    assert (
        429 in opts.retry_options.http_status_codes and 503 in opts.retry_options.http_status_codes
    )


async def test_gemini_response_parsing():
    call = SimpleNamespace(name=TOOL, args={"title": "t"})
    _, result = await gemini_call(response=gemini_response(calls=[call]))
    assert result.tool_calls == [{"name": TOOL, "args": {"title": "t"}}]
    assert result.tokens_out == 11 and result.tokens_in == 11  # 4 + 7 токенов thinking
    assert result.model_used == "gemini-3.8-flash" and result.finish == FINISH_STOP
    _, result = await gemini_call(response=gemini_response(finish="MAX_TOKENS"))
    assert result.finish == FINISH_MAX_TOKENS
    _, result = await gemini_call(response=gemini_response(finish="SAFETY"))
    assert result.finish == FINISH_REFUSAL and result.finish_detail == "SAFETY"
    _, result = await gemini_call(
        response=gemini_response(text="", block=SimpleNamespace(name="PROHIBITED_CONTENT"))
    )
    assert result.finish == FINISH_REFUSAL


# --- OpenAI (Responses API) ---------------------------------------------------------------------


def openai_response(
    output=None, text="ок", status="completed", incomplete=None, model="gpt-4o-2024-08-06"
):
    return SimpleNamespace(
        id="resp_1",
        model=model,
        output=output or [],
        output_text=text,
        status=status,
        incomplete_details=SimpleNamespace(reason=incomplete) if incomplete else None,
        usage=SimpleNamespace(input_tokens=9, output_tokens=3),
    )


async def openai_call(adapter=None, response=None, **kw):
    adapter = adapter or ADAPTERS["gpt-4o"]
    adapter.client.responses.create = AsyncMock(return_value=response or openai_response())
    result = await adapter.generate(messages=TURNS, max_tokens=16000, **kw)
    return adapter.client.responses.create.await_args.kwargs, result


async def test_openai_uses_responses_api_with_forced_function_tool():
    kwargs, _ = await openai_call(system="sys", force_tool=TOOL, timeout_s=55)
    assert kwargs["model"] == "gpt-4o" and kwargs["max_output_tokens"] == 16000
    assert kwargs["instructions"] == "sys" and kwargs["timeout"] == 55
    assert kwargs["tool_choice"] == {"type": "function", "name": TOOL}
    fn = kwargs["tools"][0]
    assert (
        fn["type"] == "function" and fn["name"] and "function" not in fn and fn["strict"] is False
    )
    assert [i["role"] for i in kwargs["input"]] == ["user", "assistant", "user"]
    assert "reasoning" not in kwargs  # gpt-4o без reasoning


async def test_openai_web_search_adds_tool_and_keeps_functions():
    kwargs, _ = await openai_call(web_search=True)
    assert kwargs["tools"][0] == {"type": "web_search"} and len(kwargs["tools"]) == 1 + len(
        ALL_TOOLS
    )
    kwargs, _ = await openai_call(tools=[])
    assert "tools" not in kwargs


async def test_openai_images_become_input_image_on_last_user_turn():
    kwargs, _ = await openai_call(
        attachments=[
            Attachment("image/png", b"x", "image"),
            Attachment("application/pdf", b"y", "pdf_document"),
        ]
    )
    last = kwargs["input"][-1]["content"]
    assert [p["type"] for p in last] == ["input_text", "input_image"]  # PDF у OpenAI не нативно


async def test_openai_json_mode():
    kwargs, _ = await openai_call(json_mode=True)
    assert kwargs["text"] == {"format": {"type": "json_object"}}


@pytest.mark.parametrize(
    ("thinking", "effort"), [("off", "none"), ("low", "low"), ("medium", "medium"), ("max", "high")]
)
async def test_openai_reasoning_effort_for_reasoning_models(thinking, effort):
    adapter = OpenAIAdapter(
        api_key="x",
        model="gpt-5.4-mini",
        caps=ModelCaps(
            thinking_mode="effort",
            forced_tool="with_thinking",
            efforts=("none", "low", "medium", "high"),
        ),
    )
    kwargs, _ = await openai_call(adapter, thinking=thinking)
    assert kwargs["reasoning"] == {"effort": effort}


def test_resolve_reasoning_effort():
    assert resolve_reasoning_effort("off", ("low", "high")) == "low"
    assert resolve_reasoning_effort("medium", ()) is None


async def test_openai_response_parsing():
    out = [SimpleNamespace(type="function_call", name=TOOL, arguments='{"title": "t"}')]
    _, result = await openai_call(response=openai_response(out, text=""))
    assert result.tool_calls == [{"name": TOOL, "args": {"title": "t"}}]
    assert result.model_used == "gpt-4o-2024-08-06"  # датированный снимок той же модели
    _, result = await openai_call(
        response=openai_response(status="incomplete", incomplete="max_output_tokens")
    )
    assert result.finish == FINISH_MAX_TOKENS
    refusal = [SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal")])]
    _, result = await openai_call(response=openai_response(refusal, text=""))
    assert result.finish == FINISH_REFUSAL


def test_openai_client_timeout_and_retries_from_config():
    client = ADAPTERS["gpt-4o"].client
    assert client.timeout == 600 and client.max_retries == 2


def test_all_adapters_expose_real_model_ids():
    assert ADAPTERS["claude-sonnet"].model == "claude-sonnet-5-5"
    assert ADAPTERS["claude-opus"].model == "claude-opus-5-5"
    assert ADAPTERS["claude-fable"].model == "claude-fable-5-1"
    assert ADAPTERS["gemini-flash-lite"].model == "gemini-3.1-flash-lite"


async def test_claude_text_blocks_are_concatenated_without_separators():
    msg = claude_message(
        [
            SimpleNamespace(type="text", text="Курс — 447,73 ₸"),
            SimpleNamespace(type="text", text=" за 1 доллар, "),
            SimpleNamespace(type="text", text="по данным Нацбанка."),
        ]
    )
    _, result = await claude_call("claude-sonnet", message=msg)
    assert result.text == "Курс — 447,73 ₸ за 1 доллар, по данным Нацбанка."
