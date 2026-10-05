"""OpenAI через Responses API — единый путь для всех моделей (в том числе gpt-4o*):
function tools, принудительный tool_choice, reasoning и web_search работают в одном
вызове (в chat.completions gpt-5.x не принимает function tools вместе с reasoning_effort
и требует max_completion_tokens вместо max_tokens)."""

import base64
import json
import time
from typing import Any, cast

from openai import AsyncOpenAI

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

_EFFORT_ORDER = ("none", "low", "medium", "high")


def _image_data_url(att: Attachment) -> str:
    b64 = base64.b64encode(att.data).decode("ascii")
    return f"data:{att.mime_type};base64,{b64}"


def resolve_reasoning_effort(abstract: str, supported: tuple[str, ...]) -> str | None:
    """Абстрактный уровень -> допустимый reasoning.effort модели (None — без reasoning)."""
    if not supported:
        return None
    wanted = {"off": "none", "low": "low", "medium": "medium", "high": "high", "max": "high"}[
        abstract
    ]
    if wanted in supported:
        return wanted
    target = _EFFORT_ORDER.index(wanted)
    return min(supported, key=lambda e: abs(_EFFORT_ORDER.index(e) - target))


class OpenAIAdapter(ModelAdapter):
    def __init__(
        self,
        api_key: str,
        model: str,
        caps: ModelCaps | None = None,
        timeout_s: float = 600.0,
        max_retries: int = 2,
    ):
        self.api_key = api_key
        self.name = model
        self.model = model
        self.caps = caps or ModelCaps(thinking_mode="none", forced_tool="with_thinking")
        # Ретраи с backoff на 429/5xx/таймауты — встроенный механизм SDK.
        self.client = AsyncOpenAI(api_key=api_key, timeout=timeout_s, max_retries=max_retries)

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

        response_tools: list[dict[str, Any]] = []
        if web_search:
            response_tools.append({"type": "web_search"})
        for t in tools if tools is not None else ALL_TOOLS:
            response_tools.append(
                {
                    "type": "function",
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                    # Наши схемы содержат необязательные поля — strict-режим их не допускает.
                    "strict": False,
                }
            )

        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": cast(Any, self._build_input(turns_from(prompt, messages), attachments)),
            "max_output_tokens": max_tokens,
        }
        if system:
            kwargs["instructions"] = system
        if response_tools:
            kwargs["tools"] = cast(Any, response_tools)
        if force_tool:
            kwargs["tool_choice"] = {"type": "function", "name": force_tool}
        effort = resolve_reasoning_effort(thinking, self.caps.efforts)
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        if json_mode:
            kwargs["text"] = {"format": {"type": "json_object"}}
        if timeout_s:
            kwargs["timeout"] = timeout_s

        response = await self.client.responses.create(**kwargs)
        latency_ms = int((time.monotonic() - started) * 1000)

        tool_calls = [
            {"name": item.name, "args": json.loads(item.arguments or "{}")}
            for item in response.output
            if item.type == "function_call"
        ]

        finish, detail = FINISH_STOP, None
        refused = any(
            part.type == "refusal"
            for item in response.output
            if item.type == "message"
            for part in item.content
        )
        incomplete = getattr(getattr(response, "incomplete_details", None), "reason", None)
        if refused or incomplete == "content_filter":
            finish, detail = FINISH_REFUSAL, "refusal" if refused else incomplete
        elif incomplete == "max_output_tokens":
            finish = FINISH_MAX_TOKENS

        usage = response.usage
        return GenerationResult(
            text=response.output_text or "",
            tokens_in=usage.input_tokens if usage else 0,
            tokens_out=usage.output_tokens if usage else 0,
            latency_ms=latency_ms,
            raw={"id": response.id, "model": response.model, "status": response.status},
            tool_calls=tool_calls,
            model_used=response.model,
            finish=finish,
            finish_detail=detail,
        )

    @staticmethod
    def _build_input(turns: list[ChatTurn], attachments: list[Attachment] | None) -> list[dict]:
        image_atts = [a for a in (attachments or []) if a.kind == "image"]
        items: list[dict] = []
        last_user = max((i for i, t in enumerate(turns) if t.role == "user"), default=-1)
        for i, turn in enumerate(turns):
            if i == last_user and image_atts:
                content: list[dict[str, Any]] = [{"type": "input_text", "text": turn.content}]
                for att in image_atts:
                    content.append(
                        {"type": "input_image", "image_url": _image_data_url(att), "detail": "auto"}
                    )
                items.append({"role": "user", "content": content})
            else:
                items.append({"role": turn.role, "content": turn.content})
        return items
