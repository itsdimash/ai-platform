import base64
import time
from typing import Any, cast

from openai import AsyncOpenAI

from .base import Attachment, GenerationResult, ModelAdapter


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
        attachments: list[Attachment] | None = None,
    ) -> GenerationResult:
        if web_search:
            # Веб-поиск и вложения одновременно не поддерживаем — комбинация
            # из ТЗ не требуется, а Responses-путь ниже собирает свой input.
            return await self._generate_with_web_search(prompt, system=system, max_tokens=max_tokens)
        return await self._generate_chat_completion(
            prompt, system=system, json_mode=json_mode, max_tokens=max_tokens, attachments=attachments
        )

    async def _generate_chat_completion(
        self,
        prompt: str,
        *,
        system: str | None,
        json_mode: bool,
        max_tokens: int,
        attachments: list[Attachment] | None = None,
    ) -> GenerationResult:
        started = time.monotonic()

        messages = []
        if system:
            messages.append({"role": "system", "content": system})

        # Только изображения. Нативного PDF в Chat Completions у OpenAI нет,
        # поэтому вызывающий код (chat_multimodal.py) для GPT-моделей
        # заранее конвертирует PDF в текст и вклеивает его в prompt —
        # сюда pdf_document-вложения доходить не должны, но на всякий
        # случай молча игнорируем всё, кроме image.
        # TODO: при желании поддержать сканы в GPT — рендерить страницы PDF
        # в PNG (pdf2image / pymupdf) и слать их как image_url здесь.
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
        # usage.prompt_tokens уже включает токены изображений — отдельно
        # ничего пересчитывать не нужно.
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

        input_items: list[dict[str, Any]] = []
        if system:
            input_items.append({"role": "developer", "content": system})
        input_items.append({"role": "user", "content": prompt})

        # SDK ожидает список TypedDict-вариантов (EasyInputMessageParam и
        # др.), а не обычный dict — ty справедливо считает их несовместимыми
        # (TypedDict запрещает деструктивные операции вроде .clear(), которые
        # разрешает dict). Структура словарей ниже полностью соответствует
        # EasyInputMessageParam ({"role": ..., "content": ...}), поэтому на
        # рантайме всё корректно — cast только снимает статическую проверку,
        # не меняя поведение.
        response = await self.client.responses.create(
            model=self.model,
            input=cast(Any, input_items),
            tools=[{"type": "web_search"}],
            max_output_tokens=max_tokens,
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = response.usage
        # usage может быть None (аналогично usage_metadata у Gemini) —
        # например, при ошибке модерации или прерванном ответе.
        tokens_in = usage.input_tokens if usage else 0
        tokens_out = usage.output_tokens if usage else 0

        return GenerationResult(
            text=response.output_text or "",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            raw={"id": response.id, "model": response.model},
        )
