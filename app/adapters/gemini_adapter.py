import time

from google import genai
from google.genai import types

from .base import Attachment, GenerationResult, ModelAdapter


class GeminiAdapter(ModelAdapter):
    def __init__(self, api_key: str, model: str = "gemini-3.6-flash", thinking_level: str = "minimal"):
        self.name = model
        self.client = genai.Client(api_key=api_key)
        self.model = model
        # Gemini 3.x модели по умолчанию используют thinking_level="medium",
        # что тратит скрытые токены рассуждений даже на тривиальные задачи
        # (замерено: 427 thinking-токенов против 12 видимых на простую
        # генерацию SQL). "minimal" — самый строгий уровень для Flash-моделей,
        # даёт нулевой расход на рассуждения (thoughts_token_count=None)
        # без потери качества на структурированных задачах вроде
        # классификации и генерации SQL. На задержку ответа это не всегда
        # влияет — судя по наблюдениям, узкое место скорее в лимитах/
        # нагрузке на стороне Gemini API, не в коде.
        self.thinking_level = thinking_level

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

        config_kwargs: dict = {
            "max_output_tokens": max_tokens,
            "thinking_config": types.ThinkingConfig(thinking_level=self.thinking_level),
        }
        if system:
            config_kwargs["system_instruction"] = system
        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"
        if web_search:
            config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]

        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=self._build_contents(prompt, attachments),
            config=types.GenerateContentConfig(**config_kwargs),
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = response.usage_metadata
        # usage_metadata может быть None (например, если Gemini прервала
        # генерацию до подсчёта метрик — пустой/заблокированный ответ).
        # prompt_token_count уже включает токены изображений и страниц PDF
        # (inline_data) при наличии usage — отдельного пересчёта не требуется.
        tokens_in = usage.prompt_token_count or 0 if usage else 0
        tokens_out = usage.candidates_token_count or 0 if usage else 0

        return GenerationResult(
            text=response.text or "",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            raw={"model": self.model},
        )

    @staticmethod
    def _build_contents(prompt: str, attachments: list[Attachment] | None):
        """Без вложений — строка (как раньше). С вложениями — список Part:
        изображения / PDF идут как inline-байты, текст промпта — последним."""
        if not attachments:
            return prompt

        parts = [
            types.Part.from_bytes(data=att.data, mime_type=att.mime_type) for att in attachments
        ]
        parts.append(types.Part.from_text(text=prompt))
        return parts
