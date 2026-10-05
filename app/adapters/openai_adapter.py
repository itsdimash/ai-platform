import base64
import json
import time
from typing import Any, cast

from openai import AsyncOpenAI

from app.tools import run_tool_calls

from .base import ALL_TOOLS, Attachment, GenerationResult, ModelAdapter


def _image_data_url(att: Attachment) -> str:
    b64 = base64.b64encode(att.data).decode("ascii")
    return f"data:{att.mime_type};base64,{b64}"


class OpenAIAdapter(ModelAdapter):
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.name = model
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model

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
        active_tools = tools if tools is not None else ALL_TOOLS
        if web_search:
            return await self._generate_with_web_search(
                prompt,
                system=system,
                max_tokens=max_tokens,
                attachments=attachments,
                tools=active_tools,
            )
        return await self._generate_chat_completion(
            prompt,
            system=system,
            json_mode=json_mode,
            max_tokens=max_tokens,
            attachments=attachments,
            tools=active_tools,
        )

    async def _generate_chat_completion(
        self,
        prompt: str,
        *,
        system: str | None,
        json_mode: bool,
        max_tokens: int,
        attachments: list[Attachment] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> GenerationResult:
        started = time.monotonic()

        messages = []
        if system:
            messages.append({"role": "system", "content": system})

        image_atts = [a for a in (attachments or []) if a.kind == "image"]

        if image_atts:
            content: list[dict] = [{"type": "text", "text": prompt}]
            for att in image_atts:
                content.append({"type": "image_url", "image_url": {"url": _image_data_url(att)}})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": prompt})

        formatted_tools = [{"type": "function", "function": t} for t in (tools or [])]

        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if formatted_tools:
            kwargs["tools"] = formatted_tools
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = await self.client.chat.completions.create(**kwargs)
        latency_ms = int((time.monotonic() - started) * 1000)

        choice = response.choices[0].message
        output_text = choice.content or ""

        tool_calls = [
            (tc.function.name, json.loads(tc.function.arguments or "{}"))
            for tc in (choice.tool_calls or [])
            if tc.type == "function"
        ]
        tool_text = await run_tool_calls(tool_calls, prompt=prompt)
        if tool_text is not None:
            output_text = tool_text

        return GenerationResult(
            text=output_text,
            tokens_in=response.usage.prompt_tokens if response.usage else 0,
            tokens_out=response.usage.completion_tokens if response.usage else 0,
            latency_ms=latency_ms,
            raw={"id": response.id, "model": response.model},
        )

    async def _generate_with_web_search(
        self,
        prompt: str,
        *,
        system: str | None,
        max_tokens: int,
        attachments: list[Attachment] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> GenerationResult:
        """Web search идёт через Responses API. Раньше эта ветка молча теряла
        function tools и вложения — теперь файловые инструменты и изображения
        работают и здесь (формат function tool в Responses API плоский, без
        обёртки {"function": ...})."""
        started = time.monotonic()

        input_items: list[dict[str, Any]] = []
        if system:
            input_items.append({"role": "developer", "content": system})

        image_atts = [a for a in (attachments or []) if a.kind == "image"]
        if image_atts:
            user_content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
            for att in image_atts:
                user_content.append(
                    {"type": "input_image", "image_url": _image_data_url(att), "detail": "auto"}
                )
            input_items.append({"role": "user", "content": user_content})
        else:
            input_items.append({"role": "user", "content": prompt})

        response_tools: list[dict[str, Any]] = [{"type": "web_search"}]
        for t in tools or []:
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

        response = await self.client.responses.create(
            model=self.model,
            input=cast(Any, input_items),
            tools=cast(Any, response_tools),
            max_output_tokens=max_tokens,
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        output_text = response.output_text or ""

        tool_calls = [
            (item.name, json.loads(item.arguments or "{}"))
            for item in response.output
            if item.type == "function_call"
        ]
        tool_text = await run_tool_calls(tool_calls, prompt=prompt)
        if tool_text is not None:
            output_text = tool_text

        usage = response.usage
        return GenerationResult(
            text=output_text,
            tokens_in=usage.input_tokens if usage else 0,
            tokens_out=usage.output_tokens if usage else 0,
            latency_ms=latency_ms,
            raw={"id": response.id, "model": response.model},
        )
