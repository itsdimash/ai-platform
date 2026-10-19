"""Этап 1: настраиваемый веб-поиск, источники, pause_turn, strict_finish. SDK замоканы."""

from types import SimpleNamespace

import pytest

from app.adapters.anthropic_adapter import MAX_PAUSE_CONTINUATIONS
from app.adapters.base import FINISH_MAX_TOKENS
from app.errors import OutputTruncated
from app.services.chat_service import _check_finish, generate_with_tools
from tests.helpers import FakeAdapter, result
from tests.test_adapter_requests import (
    ADAPTERS,
    FakeStream,
    claude_call,
    claude_message,
    gemini_call,
    gemini_response,
    openai_call,
    openai_response,
)
from tests.test_chat_service import decision


def ns(**kw):
    return SimpleNamespace(**kw)


def search_blocks():
    return [
        ns(type="server_tool_use", name="web_search", input={"query": "q"}),
        ns(
            type="web_search_tool_result",
            content=[
                ns(url="https://a.example/x", title="A"),
                ns(url="https://b.example", title="B"),
            ],
        ),
        ns(
            type="text",
            text="ответ",
            citations=[ns(type="web_search_result_location", url="https://a.example/x", title="A")],
        ),
    ]


# --- Anthropic ----------------------------------------------------------------------------


async def test_claude_max_uses_from_request_and_default():
    seen, _ = await claude_call("claude-sonnet", web_search=True, tools=[], web_search_max_uses=17)
    assert seen["kwargs"]["tools"][0]["max_uses"] == 17
    seen, _ = await claude_call("claude-sonnet", web_search=True, tools=[])
    assert seen["kwargs"]["tools"][0]["max_uses"] == 5


async def test_claude_sources_and_searches_used():
    msg = claude_message(search_blocks())
    _, res = await claude_call("claude-sonnet", message=msg, web_search=True, tools=[])
    assert [s.url for s in res.sources] == ["https://a.example/x", "https://b.example"]
    assert res.searches_used == 1 and res.text == "ответ"


async def test_claude_search_error_result_is_ignored():
    blocks = [
        ns(type="server_tool_use", name="web_search", input={}),
        ns(type="web_search_tool_result", content=ns(type="web_search_tool_result_error")),
        ns(type="text", text="x"),
    ]
    _, res = await claude_call("claude-sonnet", message=claude_message(blocks))
    assert res.sources == [] and res.searches_used == 1


def patch_sequence(adapter, messages):
    calls = []
    queue = list(messages)

    def stream(**kwargs):
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return FakeStream(queue.pop(0))

    adapter.client.messages.stream = stream
    adapter.client.beta.messages.stream = stream
    return calls


def paused(text="часть ", model="claude-sonnet-5-5"):
    return claude_message(
        [ns(type="server_tool_use", name="web_search", input={}), ns(type="text", text=text)],
        stop_reason="pause_turn",
        model=model,
    )


async def test_claude_pause_turn_is_continued_and_summed():
    adapter = ADAPTERS["claude-sonnet"]
    calls = patch_sequence(
        adapter, [paused("раз "), paused("два "), claude_message([ns(type="text", text="три")])]
    )
    res = await adapter.generate(prompt="q", web_search=True, tools=[], max_tokens=1000)
    assert len(calls) == 3
    assert res.text == "раз два три" and res.searches_used == 2
    assert res.tokens_in == 30 and res.tokens_out == 15
    assert calls[1]["messages"][-1]["role"] == "assistant"
    assert len(calls[2]["messages"]) == 3  # user + 2 ответа ассистента
    assert res.finish_detail is None


async def test_claude_pause_turn_is_bounded():
    adapter = ADAPTERS["claude-sonnet"]
    calls = patch_sequence(adapter, [paused() for _ in range(MAX_PAUSE_CONTINUATIONS + 5)])
    res = await adapter.generate(prompt="q", web_search=True, tools=[], max_tokens=1000)
    assert len(calls) == MAX_PAUSE_CONTINUATIONS + 1
    assert res.finish_detail == "pause_turn_limit"


# --- OpenAI -------------------------------------------------------------------------------


async def test_openai_max_tool_calls_only_with_web_search():
    kwargs, _ = await openai_call(web_search=True, web_search_max_uses=7)
    assert kwargs["max_tool_calls"] == 7
    kwargs, _ = await openai_call(web_search=True)
    assert "max_tool_calls" not in kwargs
    kwargs, _ = await openai_call(web_search_max_uses=7)
    assert "max_tool_calls" not in kwargs


async def test_openai_sources_and_searches_used():
    output = [
        ns(type="web_search_call"),
        ns(type="web_search_call"),
        ns(
            type="message",
            content=[
                ns(
                    type="output_text",
                    annotations=[
                        ns(type="url_citation", url="https://o.example", title="O"),
                        ns(type="url_citation", url="https://o.example", title="O"),
                        ns(type="file_citation"),
                    ],
                )
            ],
        ),
    ]
    _, res = await openai_call(response=openai_response(output=output), web_search=True)
    assert [(s.url, s.title) for s in res.sources] == [("https://o.example", "O")]
    assert res.searches_used == 2


# --- Gemini -------------------------------------------------------------------------------


async def test_gemini_grounding_sources_and_queries():
    response = gemini_response()
    response.candidates[0].grounding_metadata = ns(
        grounding_chunks=[
            ns(web=ns(uri="https://g.example", title="G")),
            ns(web=None),
        ],
        web_search_queries=["q1", "q2", "q3"],
    )
    _, res = await gemini_call(response=response, web_search=True, web_search_max_uses=2)
    assert [s.url for s in res.sources] == ["https://g.example"]
    assert res.searches_used == 3


async def test_gemini_without_grounding_has_no_sources():
    _, res = await gemini_call()
    assert res.sources == [] and res.searches_used == 0


# --- strict_finish ------------------------------------------------------------------------


def test_check_finish_text_truncation_passes_in_plain_chat_but_warns(caplog):
    cut = result(text="обрыв", finish=FINISH_MAX_TOKENS)
    with caplog.at_level("WARNING"):
        _check_finish(cut, tool_expected=False)
    assert "max_tokens" in caplog.text


def test_check_finish_strict_raises_on_text_truncation():
    with pytest.raises(OutputTruncated):
        _check_finish(
            result(text="обрыв", finish=FINISH_MAX_TOKENS), tool_expected=False, strict_finish=True
        )


async def test_generate_with_tools_strict_finish():
    adapter = FakeAdapter("claude-sonnet", [result(text="{", finish=FINISH_MAX_TOKENS)])
    dec = decision(forced=None)
    res, _ = await generate_with_tools(
        adapter, turns=[], system="S", decision=dec, attachments=None
    )
    assert res.text == "{"  # обычный чат: поведение прежнее
    adapter.script = [result(text="{", finish=FINISH_MAX_TOKENS)]
    with pytest.raises(OutputTruncated):
        await generate_with_tools(
            adapter, turns=[], system="S", decision=dec, attachments=None, strict_finish=True
        )
