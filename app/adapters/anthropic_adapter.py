import base64
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
    turns_from,
)

# Серверные fallbacks на отказы безопасности (beta). "default" сам маршрутизирует по
# категории отказа; фактически ответившая модель приходит в response.model.
REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"

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
    ) -> GenerationResult:
        started = time.monotonic()

        anthropic_tools: list[dict[str, Any]] = []
        if web_search:
            anthropic_tools.append(
                {"type": self.web_search_tool_type, "name": "web_search", "max_uses": 5}
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
        if self.caps.refusal_fallbacks:
            stream_ctx = self.client.beta.messages.stream(
                betas=[REFUSAL_FALLBACK_BETA], fallbacks="default", **kwargs
            )
        else:
            stream_ctx = self.client.messages.stream(**kwargs)
        async with stream_ctx as stream:
            response = await stream.get_final_message()
        latency_ms = int((time.monotonic() - started) * 1000)

        # Текстовые блоки (особенно с цитатами web search) — куски одного текста: склеиваем без разделителей.
        output_text = "".join(b.text for b in response.content if b.type == "text")
        # Клиентские tool_use (серверный web_search приходит как server_tool_use и сюда не
        # попадает). Исполняет их app.tools.apply_tool_calls.
        tool_calls = [
            {"name": b.name, "args": b.input or {}}
            for b in response.content
            if b.type == "tool_use"
        ]

        finish, detail = FINISH_STOP, None
        if response.stop_reason == "refusal":
            finish = FINISH_REFUSAL
            stop_details = getattr(response, "stop_details", None)
            detail = getattr(stop_details, "category", None) or "refusal"
        elif response.stop_reason == "max_tokens":
            finish = FINISH_MAX_TOKENS

        return GenerationResult(
            text=output_text,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            latency_ms=latency_ms,
            raw={"id": response.id, "model": response.model, "stop_reason": response.stop_reason},
            tool_calls=tool_calls,
            model_used=response.model,
            finish=finish,
            finish_detail=detail,
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
