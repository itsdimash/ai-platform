import base64
import logging
import time
from typing import Any

from anthropic import AsyncAnthropic

from .base import (
    ALL_TOOLS,
    FINISH_MAX_TOKENS,
    FINISH_REFUSAL,
    FINISH_STOP,
    Attachment,
    ChatTurn,
    GenerationResult,
    ModelAdapter,
    ModelCaps,
    SourceRef,
    add_source,
    turns_from,
)

# Серверные fallbacks на отказы безопасности (beta). "default" сам маршрутизирует по
# категории отказа; фактически ответившая модель приходит в response.model.
REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"

logger = logging.getLogger(__name__)

# Серверный цикл web_search Anthropic может вернуть stop_reason="pause_turn": ход нужно
# продолжить, отправив ответ ассистента обратно. Потолок продолжений на один вызов generate().
MAX_PAUSE_CONTINUATIONS = 5
# Число поисков в запросе, если вызывающий не задал web_search_max_uses.
DEFAULT_WEB_SEARCH_MAX_USES = 5

# Бюджеты thinking для моделей с budget_tokens (Haiku 4.5).
_BUDGET_TOKENS = {"low": 2000, "medium": 6000, "high": 16000, "max": 16000}
_MIN_BUDGET = 1024


class AnthropicAdapter(ModelAdapter):
    def __init__(
        self,
        api_key: str,
        model: str,
        caps: ModelCaps | None = None,
        web_search_tool_type: str = "web_search_20250305",
        timeout_s: float = 600.0,
        max_retries: int = 2,
    ):
        self.name = model
        self.model = model
        self.caps = caps or ModelCaps(thinking_mode="adaptive", thinking_off="unsupported")
        # Ретраи с backoff на 429/5xx/таймауты — встроенный механизм SDK.
        self.client = AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=max_retries)
        self.web_search_tool_type = web_search_tool_type

    # --- параметры thinking ----------------------------------------------------

    def thinking_params(
        self, thinking: str, max_tokens: int, *, forcing_tool: bool
    ) -> tuple[dict | None, dict | None]:
        """(thinking, output_config) для заданного абстрактного уровня."""
        caps = self.caps
        if forcing_tool and caps.forced_tool == "no_thinking":
            thinking = "off"  # Haiku: принудительный tool несовместим с thinking
        if caps.thinking_mode == "budget":
            if thinking == "off":
                return None, None
            budget = min(_BUDGET_TOKENS.get(thinking, 6000), max_tokens - _MIN_BUDGET)
            if budget < _MIN_BUDGET:
                return None, None
            return {"type": "enabled", "budget_tokens": budget}, None
        if caps.thinking_mode == "adaptive":
            if thinking == "off":
                if caps.thinking_off == "disabled":
                    return {"type": "disabled"}, None
                if caps.thinking_off == "between_tools":
                    return {"type": "between_tools"}, {"effort": "low"}
                return {"type": "adaptive"}, {"effort": "low"}  # выключить нельзя
            return {"type": "adaptive"}, {"effort": thinking}
        return None, None

    # --- генерация -----------------------------------------------------------------

    async def generate(
        self,
        prompt: str | None = None,
        *,
        messages: list[ChatTurn] | None = None,
        system: str | None = None,
        json_mode: bool = False,
        web_search: bool = False,
        max_tokens: int = 8192,
        attachments: list[Attachment] | None = None,
        tools: list[dict[str, Any]] | None = None,
        force_tool: str | None = None,
        thinking: str = "off",
        timeout_s: float | None = None,
        web_search_max_uses: int | None = None,
    ) -> GenerationResult:
        started = time.monotonic()

        anthropic_tools: list[dict[str, Any]] = []
        if web_search:
            anthropic_tools.append(
                {
                    "type": self.web_search_tool_type,
                    "name": "web_search",
                    "max_uses": (
                        web_search_max_uses
                        if web_search_max_uses is not None
                        else DEFAULT_WEB_SEARCH_MAX_USES
                    ),
                }
            )
        for t in tools if tools is not None else ALL_TOOLS:
            anthropic_tools.append(
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["parameters"],
                }
            )

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": self._build_messages(turns_from(prompt, messages), attachments),
        }
        if anthropic_tools:  # пустой список ни к чему
            kwargs["tools"] = anthropic_tools
        if force_tool:
            kwargs["tool_choice"] = {"type": "tool", "name": force_tool}
        if system:
            kwargs["system"] = system
        thinking_param, output_config = self.thinking_params(
            thinking, max_tokens, forcing_tool=bool(force_tool)
        )
        if thinking_param:
            kwargs["thinking"] = thinking_param
        if output_config:
            kwargs["output_config"] = output_config
        if timeout_s:
            kwargs["timeout"] = timeout_s

        # Стриминговый хелпер внутри (SDK отвергает нестриминговые вызовы с большим
        # max_tokens); наружу возвращается итоговое сообщение — API остаётся не-стриминговым.
        # pause_turn: продолжаем ход (до MAX_PAUSE_CONTINUATIONS раз), суммируя токены и текст.
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        sources: list[SourceRef] = []
        searches_used = 0
        tokens_in = tokens_out = 0
        pause_limit_hit = False
        continuations = 0
        while True:
            if self.caps.refusal_fallbacks:
                stream_ctx = self.client.beta.messages.stream(
                    betas=[REFUSAL_FALLBACK_BETA], fallbacks="default", **kwargs
                )
            else:
                stream_ctx = self.client.messages.stream(**kwargs)
            async with stream_ctx as stream:
                response = await stream.get_final_message()

            tokens_in += response.usage.input_tokens
            tokens_out += response.usage.output_tokens
            # Текстовые блоки (особенно с цитатами web search) — куски одного текста:
            # склеиваем без разделителей.
            text_parts.extend(b.text for b in response.content if b.type == "text")
            # Клиентские tool_use (серверный web_search приходит как server_tool_use и сюда
            # не попадает). Исполняет их app.tools.apply_tool_calls.
            tool_calls.extend(
                {"name": b.name, "args": b.input or {}}
                for b in response.content
                if b.type == "tool_use"
            )
            searches_used += _collect_web_search(response.content, sources)

            if response.stop_reason != "pause_turn":
                break
            if continuations >= MAX_PAUSE_CONTINUATIONS:
                pause_limit_hit = True
                logger.warning(
                    "pause_turn: continuation limit (%d) reached for %s",
                    MAX_PAUSE_CONTINUATIONS,
                    self.model,
                )
                break
            continuations += 1
            kwargs["messages"] = [
                *kwargs["messages"],
                {"role": "assistant", "content": response.content},
            ]

        latency_ms = int((time.monotonic() - started) * 1000)
        output_text = "".join(text_parts)

        finish, detail = FINISH_STOP, None
        if response.stop_reason == "refusal":
            finish = FINISH_REFUSAL
            stop_details = getattr(response, "stop_details", None)
            detail = getattr(stop_details, "category", None) or "refusal"
        elif response.stop_reason == "max_tokens":
            finish = FINISH_MAX_TOKENS
        elif pause_limit_hit:
            detail = "pause_turn_limit"

        return GenerationResult(
            text=output_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            raw={"id": response.id, "model": response.model, "stop_reason": response.stop_reason},
            tool_calls=tool_calls,
            model_used=response.model,
            finish=finish,
            finish_detail=detail,
            sources=sources,
            searches_used=searches_used,
        )

    @staticmethod
    def _build_messages(turns: list[ChatTurn], attachments: list[Attachment] | None) -> list[dict]:
        out: list[dict] = []
        last_user = max((i for i, t in enumerate(turns) if t.role == "user"), default=-1)
        for i, turn in enumerate(turns):
            if i == last_user and attachments:
                out.append(
                    {
                        "role": "user",
                        "content": _content_with_attachments(turn.content, attachments),
                    }
                )
            else:
                out.append({"role": turn.role, "content": turn.content})
        return out


def _collect_web_search(content: list, sources: list[SourceRef]) -> int:
    """Источники из web_search_tool_result и цитат текстовых блоков; возвращает число
    выполненных поисков (server_tool_use с name == "web_search")."""
    searches = 0
    for block in content:
        kind = getattr(block, "type", None)
        if kind == "server_tool_use" and getattr(block, "name", None) == "web_search":
            searches += 1
        elif kind == "web_search_tool_result":
            results = getattr(block, "content", None)
            if isinstance(results, list):  # при ошибке поиска content — объект ошибки
                for r in results:
                    add_source(sources, getattr(r, "url", None), getattr(r, "title", ""))
        elif kind == "text":
            for cit in getattr(block, "citations", None) or []:
                if getattr(cit, "type", None) == "web_search_result_location":
                    add_source(sources, getattr(cit, "url", None), getattr(cit, "title", ""))
    return searches


def _content_with_attachments(text: str, attachments: list[Attachment]) -> list[dict]:
    blocks: list[dict] = [{"type": "text", "text": text}]
    for att in attachments:
        b64 = base64.b64encode(att.data).decode("ascii")
        if att.kind == "image":
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": att.mime_type, "data": b64},
                }
            )
        elif att.kind == "pdf_document":
            blocks.append(
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
                }
            )
    return blocks
