import time

from anthropic import AsyncAnthropic

from .base import GenerationResult, ModelAdapter


class AnthropicAdapter(ModelAdapter):
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
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
    ) -> GenerationResult:
        started = time.monotonic()

        kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
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
        return GenerationResult(
            text="\n".join(text_blocks),
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            latency_ms=latency_ms,
            raw={"id": response.id, "model": response.model, "stop_reason": response.stop_reason},
        )
