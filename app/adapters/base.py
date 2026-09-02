from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class GenerationResult:
    """Единый формат ответа от любого провайдера."""

    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    raw: dict = field(default_factory=dict)


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
    ) -> GenerationResult:
        """Отправляет запрос модели и возвращает нормализованный результат.

        json_mode — форсирует структурированный JSON-вывод (для классификатора
        и db_query, где важен предсказуемый формат ответа).
        web_search — включает встроенный tool веб-поиска у провайдера, если
        он поддерживается (нужно для промпт-шаблонов вроде тендерных).
        """
        raise NotImplementedError
