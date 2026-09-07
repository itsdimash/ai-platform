import base64
import time

from anthropic import AsyncAnthropic

from .base import Attachment, GenerationResult, ModelAdapter


class AnthropicAdapter(ModelAdapter):
    def __init__(self, api_key: str, model: str = "claude-sonnet-5"):
        self.name = model
        self.client = AsyncAnthropic(api_key=api_key)
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
    ) -> GenerationResult:
        started = time.monotonic()

        kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": self._build_content(prompt, attachments)}],
        }
        if system:
            kwargs["system"] = system
        if json_mode:
            # Claude не имеет отдельного response_format — просим JSON явно
            # в system-промпте на уровне вызывающего кода (classifier/db_query).
            pass
        if web_search:
            kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]

        response = await self.client.messages.create(**kwargs)
        latency_ms = int((time.monotonic() - started) * 1000)

        text_blocks = [b.text for b in response.content if b.type == "text"]
        # usage.input_tokens уже включает токены изображений и страниц PDF —
        # отдельного пересчёта не требуется.
        return GenerationResult(
            text="\n".join(text_blocks),
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            latency_ms=latency_ms,
            raw={"id": response.id, "model": response.model, "stop_reason": response.stop_reason},
        )

    @staticmethod
    def _build_content(prompt: str, attachments: list[Attachment] | None) -> str | list[dict]:
        """Без вложений — обычная строка (как раньше). С вложениями — список
        content-блоков: текст + image / document (нативный PDF)."""
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
