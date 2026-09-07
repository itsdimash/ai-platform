from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class GenerationResult:
    """Единый формат ответа от любого провайдера."""

    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    raw: dict = field(default_factory=dict)


@dataclass
class Attachment:
    """Одно бинарное вложение, передаваемое модели «как есть» (native
    vision / native PDF), без предварительной text-extraction.

    kind:
    - "image"        — растровое изображение (png/jpeg/webp), уходит
      в vision-часть запроса провайдера;
    - "pdf_document" — PDF целиком, уходит document-блоком (умеют
      Anthropic и Gemini; для OpenAI Chat Completions нативного PDF нет —
      см. комментарии в openai_adapter.py).

    data — сырые байты файла (не base64). Кодирование в base64, где это
    нужно провайдеру, делает конкретный адаптер.
    """

    mime_type: str
    data: bytes
    kind: Literal["image", "pdf_document"]


class ModelAdapter(ABC):
    """Общий интерфейс ко всем моделям. Роутер и классификатор работают
    только с этим интерфейсом и не знают, какой провайдер за ним стоит —
    тот же принцип, что и у OneCClient в ERP (один интерфейс, разные
    реализации)."""

    name: str

    @abstractmethod
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
        """Отправляет запрос модели и возвращает нормализованный результат.

        json_mode — форсирует структурированный JSON-вывод (для классификатора
        и db_query, где важен предсказуемый формат ответа).
        web_search — включает встроенный tool веб-поиска у провайдера, если
        он поддерживается (нужно для промпт-шаблонов вроде тендерных).
        attachments — бинарные вложения (изображения / PDF), которые модель
        обрабатывает нативно. None (дефолт) — обычный текстовый запрос, как
        у классификатора, db_query и старого /v1/chat: их вызовы не меняются.
        Расход токенов на вложения провайдер уже учитывает в usage.*_tokens —
        отдельного пересчёта в адаптерах не требуется.
        """
        raise NotImplementedError
