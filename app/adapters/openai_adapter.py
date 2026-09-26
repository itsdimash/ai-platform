import base64
import json
import time
from typing import Any, cast

from openai import AsyncOpenAI

from app.utils.docx_builder import create_document_file
from app.utils.image_builder import generate_and_save_image
from app.utils.pptx_builder import create_presentation_file
from app.utils.xlsx_builder import create_spreadsheet_file
from .base import ALL_TOOLS, Attachment, GenerationResult, ModelAdapter


class OpenAIAdapter(ModelAdapter):
    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
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
        if web_search:
            return await self._generate_with_web_search(prompt, system=system, max_tokens=max_tokens)
        return await self._generate_chat_completion(
            prompt,
            system=system,
            json_mode=json_mode,
            max_tokens=max_tokens,
            attachments=attachments,
            tools=tools if tools is not None else ALL_TOOLS,
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
                b64 = base64.b64encode(att.data).decode("ascii")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{att.mime_type};base64,{b64}"},
                    }
                )
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

        # Обработка вызовов функций (Tool Calls)
        if choice.tool_calls:
            for tool_call in choice.tool_calls:
                func_name = tool_call.function.name
                args = json.loads(tool_call.function.arguments)

                if func_name == "generate_presentation":
                    file_url = create_presentation_file(
                        title=args.get("title", "Презентация"),
                        subtitle=args.get("subtitle", ""),
                        slides_data=args.get("slides", []),
                    )
                    output_text = (
                        f"📊 Готово! Я сформировал презентацию «**{args.get('title')}**».\n\n"
                        f"[📥 Скачать презентацию (.pptx)]({file_url})"
                    )

                elif func_name == "generate_document":
                    file_url = create_document_file(
                        title=args.get("title", "Документ"),
                        sections=args.get("sections", []),
                    )
                    output_text = (
                        f"📄 Готово! Я сформировал документ «**{args.get('title')}**».\n\n"
                        f"[📥 Скачать документ (.docx)]({file_url})"
                    )

                elif func_name == "generate_spreadsheet":
                    file_url = create_spreadsheet_file(
                        filename=args.get("filename", "Таблица"),
                        sheets=args.get("sheets", []),
                    )
                    output_text = (
                        f"📈 Готово! Я сформировал таблицу «**{args.get('filename')}**».\n\n"
                        f"[📥 Скачать таблицу (.xlsx)]({file_url})"
                    )

                elif func_name == "generate_image":
                    # ИСПРАВЛЕНО: generate_and_save_image() не принимает api_key
                    # как параметр — падало с TypeError на каждом вызове.
                    img_url = await generate_and_save_image(
                        prompt=args.get("prompt", prompt),
                        size=args.get("size", "1024x1024"),
                    )
                    output_text = f"🎨 Вот изображение по вашему запросу:\n\n![Сгенерированное изображение]({img_url})"

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
    ) -> GenerationResult:
        started = time.monotonic()

        input_items: list[dict[str, Any]] = []
        if system:
            input_items.append({"role": "developer", "content": system})
        input_items.append({"role": "user", "content": prompt})

        response = await self.client.responses.create(
            model=self.model,
            input=cast(Any, input_items),
            tools=[{"type": "web_search"}],
            max_output_tokens=max_tokens,
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = response.usage
        tokens_in = usage.input_tokens if usage else 0
        tokens_out = usage.output_tokens if usage else 0

        return GenerationResult(
            text=response.output_text or "",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            raw={"id": response.id, "model": response.model},
        )
