import time

from google import genai
from google.genai import types

from .base import GenerationResult, ModelAdapter


class GeminiAdapter(ModelAdapter):
    def __init__(self, api_key: str, model: str = "gemini-3.6-flash"):
        self.name = model
        self.client = genai.Client(api_key=api_key)
        self.model = model

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

        config_kwargs: dict = {"max_output_tokens": max_tokens}
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
