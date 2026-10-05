import time
from typing import Any

from google import genai
from google.genai import types

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

_LEVEL_ORDER = ("minimal", "low", "medium", "high")
_RETRY_STATUS_CODES = [429, 500, 502, 503, 504]
# finish_reason, означающие отказ/блокировку по безопасности.
_REFUSAL_REASONS = {
    "SAFETY",
    "PROHIBITED_CONTENT",
    "BLOCKLIST",
    "SPII",
    "IMAGE_SAFETY",
    "RECITATION",
}


def resolve_thinking_level(abstract: str, supported: tuple[str, ...]) -> str | None:
    """Абстрактный уровень (off|low|medium|high|max) -> допустимый thinking_level модели.
    Допустимые уровни у каждой модели свои (3.7/3.8-flash и pro не принимают minimal),
    поэтому берём ближайший из разрешённых конфигом."""
    if not supported:
        return None
    wanted = {"off": "minimal", "low": "low", "medium": "medium", "high": "high", "max": "high"}[
        abstract
    ]
    if wanted in supported:
        return wanted
    target = _LEVEL_ORDER.index(wanted)
    return min(supported, key=lambda lvl: abs(_LEVEL_ORDER.index(lvl) - target))


class GeminiAdapter(ModelAdapter):
    def __init__(
        self,
        api_key: str,
        model: str,
        caps: ModelCaps | None = None,
        timeout_s: float = 600.0,
        max_retries: int = 2,
    ):
        self.name = model
        self.model = model
        self.caps = caps or ModelCaps(
            thinking_mode="level",
            thinking_levels=("low", "medium", "high"),
            forced_tool="with_thinking",
        )
        self._retry = types.HttpRetryOptions(
            attempts=max_retries + 1,  # включая первую попытку
            http_status_codes=_RETRY_STATUS_CODES,
        )
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=int(timeout_s * 1000), retry_options=self._retry
            ),
        )

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

        tool_list: list[types.Tool] = []
        if web_search:
            # Google Search вместе с function tools Gemini не принимает без отдельного
            # флага (400) — в поисковом запросе функции файловых инструментов не нужны.
            tool_list.append(types.Tool(google_search=types.GoogleSearch()))
        else:
            active_tools = tools if tools is not None else ALL_TOOLS
            if active_tools:
                tool_list.append(
                    types.Tool(
                        function_declarations=[
                            types.FunctionDeclaration(
                                name=t["name"],
                                description=t["description"],
                                parameters=t["parameters"],
                            )
                            for t in active_tools
                        ]
                    )
                )

        config_kwargs: dict[str, Any] = {"max_output_tokens": max_tokens}
        level = resolve_thinking_level(thinking, self.caps.thinking_levels)
        if level:
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=level)
        # tools добавляются, только если реально есть: Gemini запрещает связку
        # response_mime_type="application/json" с function declarations.
        if tool_list and not json_mode:
            config_kwargs["tools"] = tool_list
            if force_tool and not web_search:
                config_kwargs["tool_config"] = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode=types.FunctionCallingConfigMode.ANY,
                        allowed_function_names=[force_tool],
                    )
                )
        if system:
            config_kwargs["system_instruction"] = system
        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"
        if timeout_s:
            config_kwargs["http_options"] = types.HttpOptions(
                timeout=int(timeout_s * 1000), retry_options=self._retry
            )

        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=self._build_contents(turns_from(prompt, messages), attachments),
            config=types.GenerateContentConfig(**config_kwargs),
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        candidate = response.candidates[0] if response.candidates else None
        parts = (candidate.content.parts if candidate and candidate.content else None) or []
        output_text = "".join(p.text for p in parts if p.text and not p.thought)
        tool_calls = [
            {"name": call.name or "", "args": dict(call.args or {})}
            for call in (response.function_calls or [])
        ]

        finish, detail = FINISH_STOP, None
        reason = getattr(candidate.finish_reason, "name", None) if candidate else None
        block = getattr(response.prompt_feedback, "block_reason", None)
        if block or reason in _REFUSAL_REASONS:
            finish, detail = FINISH_REFUSAL, str(getattr(block, "name", block) or reason)
        elif reason == "MAX_TOKENS":
            finish = FINISH_MAX_TOKENS
        elif reason not in (None, "STOP"):
            detail = reason

        usage = response.usage_metadata
        tokens_in = (usage.prompt_token_count or 0) if usage else 0
        # Токены thinking тарифицируются как выходные.
        tokens_out = (
            ((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0))
            if usage
            else 0
        )

        return GenerationResult(
            text=output_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            raw={"model": self.model, "finish_reason": reason},
            tool_calls=tool_calls,
            model_used=response.model_version or self.model,
            finish=finish,
            finish_detail=detail,
        )

    @staticmethod
    def _build_contents(
        turns: list[ChatTurn], attachments: list[Attachment] | None
    ) -> list[types.Content]:
        contents: list[types.Content] = []
        last_user = max((i for i, t in enumerate(turns) if t.role == "user"), default=-1)
        for i, turn in enumerate(turns):
            parts: list[types.Part] = []
            if i == last_user and attachments:
                parts += [
                    types.Part.from_bytes(data=att.data, mime_type=att.mime_type)
                    for att in attachments
                ]
            parts.append(types.Part.from_text(text=turn.content))
            contents.append(
                types.Content(role="user" if turn.role == "user" else "model", parts=parts)
            )
        return contents
