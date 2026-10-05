import base64
import time
from typing import Any

from anthropic import AsyncAnthropic

from .base import ALL_TOOLS, Attachment, GenerationResult, ModelAdapter


class AnthropicAdapter(ModelAdapter):
    def __init__(
        self,
        api_key: str,
        model: str,
        web_search_tool_type: str = "web_search_20250305",
    ):
        self.name = model
        self.model = model
        self.client = AsyncAnthropic(api_key=api_key)
        self.web_search_tool_type = web_search_tool_type

    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        web_search: bool = False,
        max_tokens: int = 2048,
        attachments: list[Attachment] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> GenerationResult:
        started = time.monotonic()

        anthropic_tools: list[dict[str, Any]] = []
        if web_search:
            anthropic_tools.append({"type": self.web_search_tool_type, "name": "web_search"})

        active_tools = tools if tools is not None else ALL_TOOLS
        for t in active_tools:
            anthropic_tools.append(
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["parameters"],
                }
            )

        kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": self._build_content(prompt, attachments)}],
        }
        # tools передаём только если они есть: пустой список ни к чему.
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
        if system:
            kwargs["system"] = system

        response = await self.client.messages.create(**kwargs)
        latency_ms = int((time.monotonic() - started) * 1000)

        text_blocks = [b.text for b in response.content if b.type == "text"]
        output_text = "\n".join(text_blocks)

        # Клиентские tool_use (серверный web_search приходит как server_tool_use
        # и сюда не попадает). Исполняет их app.tools.apply_tool_calls.
        tool_calls = [
            {"name": b.name, "args": b.input or {}}
            for b in response.content
            if b.type == "tool_use"
        ]

        return GenerationResult(
            text=output_text,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            latency_ms=latency_ms,
            raw={"id": response.id, "model": response.model, "stop_reason": response.stop_reason},
            tool_calls=tool_calls,
        )

    @staticmethod
    def _build_content(prompt: str, attachments: list[Attachment] | None) -> str | list[dict]:
        if not attachments:
            return prompt

        blocks: list[dict] = [{"type": "text", "text": prompt}]
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
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": b64,
                        },
                    }
                )
        return blocks
