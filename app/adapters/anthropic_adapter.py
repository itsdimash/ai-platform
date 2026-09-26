import base64
import time
from typing import Any

from anthropic import AsyncAnthropic

from app.utils.docx_builder import create_document_file
from app.utils.image_builder import generate_and_save_image
from app.utils.pptx_builder import create_presentation_file
from app.utils.xlsx_builder import create_spreadsheet_file
from .base import ALL_TOOLS, Attachment, GenerationResult, ModelAdapter


class AnthropicAdapter(ModelAdapter):
    def __init__(self, api_key: str, openai_api_key: str | None = None, model: str = "claude-sonnet-5"):
        self.name = model
        self.client = AsyncAnthropic(api_key=api_key)
        self.openai_api_key = openai_api_key or api_key
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
        started = time.monotonic()

        anthropic_tools = []
        if web_search:
            anthropic_tools.append({"type": "web_search_20250305", "name": "web_search"})

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
            "tools": anthropic_tools,
        }
        if system:
            kwargs["system"] = system

        response = await self.client.messages.create(**kwargs)
        latency_ms = int((time.monotonic() - started) * 1000)

        text_blocks = [b.text for b in response.content if b.type == "text"]
        output_text = "\n".join(text_blocks)

        # Обработка вызова функций в Claude
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        for tool_call in tool_uses:
            args = tool_call.input or {}
            if tool_call.name == "generate_presentation":
                file_url = create_presentation_file(
                    title=args.get("title", "Презентация"),
                    subtitle=args.get("subtitle", ""),
                    slides_data=args.get("slides", []),
                )
                output_text = (
                    f"📊 Готово! Я сформировал презентацию «**{args.get('title')}**».\n\n"
                    f"[📥 Скачать презентацию (.pptx)]({file_url})"
                )
            elif tool_call.name == "generate_document":
                file_url = create_document_file(
                    title=args.get("title", "Документ"),
                    sections=args.get("sections", []),
                )
                output_text = (
                    f"📄 Готово! Я сформировал документ «**{args.get('title')}**».\n\n"
                    f"[📥 Скачать документ (.docx)]({file_url})"
                )
            elif tool_call.name == "generate_spreadsheet":
                file_url = create_spreadsheet_file(
                    filename=args.get("filename", "Таблица"),
                    sheets=args.get("sheets", []),
                )
                output_text = (
                    f"📈 Готово! Я сформировал таблицу «**{args.get('filename')}**».\n\n"
                    f"[📥 Скачать таблицу (.xlsx)]({file_url})"
                )
            elif tool_call.name == "generate_image":
                # ИСПРАВЛЕНО: generate_and_save_image() не принимает api_key —
                # он сам берёт ключ через _get_openai_api_key() внутри
                # image_builder.py. Передача api_key= сюда роняла КАЖДЫЙ
                # вызов генерации картинки из Claude с TypeError.
                img_url = await generate_and_save_image(
                    prompt=args.get("prompt", prompt),
                    size=args.get("size", "1024x1024"),
                )
                output_text = f"🎨 Вот изображение по вашему запросу:\n\n![Сгенерированное изображение]({img_url})"

        return GenerationResult(
            text=output_text,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            latency_ms=latency_ms,
            raw={"id": response.id, "model": response.model, "stop_reason": response.stop_reason},
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
