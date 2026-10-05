from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from app.tools import ALL_TOOLS

# Нормализованные причины остановки (адаптеры приводят ответы провайдеров к ним).
FINISH_STOP = "stop"
FINISH_MAX_TOKENS = "max_tokens"
FINISH_REFUSAL = "refusal"


@dataclass
class ChatTurn:
    """Реплика диалога для провайдера. Роль БД «ai» маппится в assistant выше."""

    role: Literal["user", "assistant"]
    content: str


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
    # Модель, фактически обслужившая запрос, по ответу провайдера (может отличаться от
    # запрошенной при серверном fallback или быть датированным снимком).
    model_used: str | None = None
    finish: str = FINISH_STOP
    finish_detail: str | None = None  # категория отказа / сырой finish_reason — для лога
    image_model: str | None = None  # реальный id модели картинок, если создавалась картинка


@dataclass
class Attachment:
    mime_type: str
    data: bytes
    kind: Literal["image", "pdf_document"]


@dataclass
class ModelCaps:
    """Проверенные возможности модели (config.yaml -> models.<name>.caps)."""

    thinking_mode: str = "none"  # adaptive | budget | level | effort | none
    thinking_off: str = "unsupported"  # disabled | between_tools | unsupported (adaptive)
    forced_tool: str = "unsupported"  # with_thinking | no_thinking | unsupported
    thinking_levels: tuple[str, ...] = ()
    efforts: tuple[str, ...] = ()
    max_output_tokens: int = 8192
    refusal_fallbacks: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "ModelCaps":
        return cls(
            thinking_mode=data.get("thinking_mode", "none"),
            thinking_off=data.get("thinking_off", "unsupported"),
            forced_tool=data.get("forced_tool", "unsupported"),
            thinking_levels=tuple(data.get("thinking_levels", ())),
            efforts=tuple(data.get("efforts", ())),
            max_output_tokens=data.get("max_output_tokens", 8192),
            refusal_fallbacks=bool(data.get("refusal_fallbacks", False)),
        )

    def can_force_tool(self, thinking: str = "off") -> bool:
        """Принимает ли модель принудительный tool_choice. no_thinking (Haiku) — да, но
        адаптер при этом отключает thinking; unsupported (Claude 5.5, Fable) — нет."""
        return self.forced_tool in ("with_thinking", "no_thinking")


def turns_from(prompt: str | None, messages: list[ChatTurn] | None) -> list[ChatTurn]:
    """Совместимость: старый вызов generate(prompt=...) -> одна реплика пользователя."""
    if messages:
        return list(messages)
    return [ChatTurn("user", prompt or "")]


def same_model(requested: str, actual: str | None) -> bool:
    """Фактическая модель совпадает с запрошенной (датированные снимки вроде
    gpt-4o-2024-08-06 для gpt-4o — это та же модель)."""
    return not actual or actual.startswith(requested) or requested.startswith(actual)


__all__ = [
    "ALL_TOOLS",
    "FINISH_MAX_TOKENS",
    "FINISH_REFUSAL",
    "FINISH_STOP",
    "Attachment",
    "ChatTurn",
    "GenerationResult",
    "ModelAdapter",
    "ModelCaps",
    "same_model",
    "turns_from",
]


class ModelAdapter(ABC):
    name: str
    # Реальный ID модели у провайдера (из config.yaml -> models). Логическое имя
    # ("claude-sonnet") остаётся ключом роутера и значением параметра API `model`.
    model: str
    caps: ModelCaps

    @abstractmethod
    async def generate(
        self,
        prompt: str | None = None,
        *,
        messages: list[ChatTurn] | None = None,
        system: str | None = None,
        json_mode: bool = False,
        web_search: bool = False,
        max_tokens: int = 8192,
        attachments: list[Attachment] | None = None,
        tools: list[dict[str, Any]] | None = None,
        force_tool: str | None = None,
        thinking: str = "off",
        timeout_s: float | None = None,
    ) -> GenerationResult:
        """prompt= (строка) — старый путь; messages= — реплики с ролями. Вложения
        относятся к последней реплике пользователя. force_tool — имя инструмента,
        вызов которого нужно принудить (адаптер обязан поддерживать это при
        caps.can_force_tool). thinking — абстрактный уровень off|low|medium|high|max."""
        raise NotImplementedError
