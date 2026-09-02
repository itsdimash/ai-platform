import time

from openai import AsyncOpenAI

from .base import GenerationResult, ModelAdapter


class OpenAIAdapter(ModelAdapter):
    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
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
    ) -> GenerationResult:
        started = time.monotonic()

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if web_search:
            # Responses API поддерживает server-side web_search tool.
            # Для chat.completions это потребует отдельного вызова через
            # client.responses.create — оставлено как явный TODO, чтобы не
            # маскировать разницу в API под одинаковым вызовом.
            kwargs["tools"] = [{"type": "web_search"}]

        response = await self.client.chat.completions.create(**kwargs)
        latency_ms = int((time.monotonic() - started) * 1000)

        choice = response.choices[0].message
        return GenerationResult(
            text=choice.content or "",
            tokens_in=response.usage.prompt_tokens,
            tokens_out=response.usage.completion_tokens,
            latency_ms=latency_ms,
            raw={"id": response.id, "model": response.model},
        )
