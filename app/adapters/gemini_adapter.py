import time

from google import genai
from google.genai import types

from .base import GenerationResult, ModelAdapter


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
            contents=prompt,
            config=types.GenerateContentConfig(**config_kwargs),
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = response.usage_metadata
        return GenerationResult(
            text=response.text or "",
            tokens_in=usage.prompt_token_count or 0,
            tokens_out=usage.candidates_token_count or 0,
            latency_ms=latency_ms,
            raw={"model": self.model},
        )
