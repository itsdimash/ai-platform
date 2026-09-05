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
        if web_search:
            return await self._generate_with_web_search(prompt, system=system, max_tokens=max_tokens)
        return await self._generate_chat_completion(
            prompt, system=system, json_mode=json_mode, max_tokens=max_tokens
        )

    async def _generate_chat_completion(
        self,
        prompt: str,
        *,
        system: str | None,
        json_mode: bool,
        max_tokens: int,
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

    async def _generate_with_web_search(
        self,
        prompt: str,
        *,
        system: str | None,
        max_tokens: int,
    ) -> GenerationResult:
        """Веб-поиск доступен только через Responses API (client.responses.create),
        не через Chat Completions — tools=[{"type": "web_search"}] в
        chat.completions.create не поддерживается и будет отклонён API.
        Chat Completions поддерживает поиск лишь косвенно, через отдельные
        модели gpt-4o-search-preview/gpt-4o-mini-search-preview с параметром
        web_search_options, что не подходит для произвольной модели вроде
        обычного gpt-4o, используемого в router/config.yaml."""

        started = time.monotonic()

        input_items = []
        if system:
            input_items.append({"role": "developer", "content": system})
        input_items.append({"role": "user", "content": prompt})

        response = await self.client.responses.create(
            model=self.model,
            input=input_items,
            tools=[{"type": "web_search"}],
            max_output_tokens=max_tokens,
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        return GenerationResult(
            text=response.output_text or "",
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            latency_ms=latency_ms,
            raw={"id": response.id, "model": response.model},
        )
