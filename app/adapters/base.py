from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from app.tools import ALL_TOOLS


@dataclass
class GenerationResult:
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    raw: dict = field(default_factory=dict)
    # Вызовы наших инструментов из ответа модели: [{"name": str, "args": dict}].
    # Адаптеры их только достают; исполняет app.tools.apply_tool_calls.
    tool_calls: list[dict] = field(default_factory=list)
    # Записи вложений (формат БД, без url), заполняются после исполнения tools.
    attachments: list[dict] = field(default_factory=list)


@dataclass
class Attachment:
    mime_type: str
    data: bytes
    kind: Literal["image", "pdf_document"]


__all__ = ["ALL_TOOLS", "Attachment", "GenerationResult", "ModelAdapter"]


class ModelAdapter(ABC):
    name: str
    # Реальный ID модели у провайдера (из config.yaml -> models). Именно он
    # пишется в model_used в логах/истории; логическое имя ("claude-sonnet")
    # остаётся только внутренним ключом роутера и API-параметра `model`.
    model: str

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
        tools: list[dict[str, Any]] | None = None,
    ) -> GenerationResult:
        raise NotImplementedError
