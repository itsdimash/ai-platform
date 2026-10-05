from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


@lru_cache
def load_config(path: Path = _CONFIG_PATH) -> dict:
    """YAML-конфиг роутера целиком (routing_rules, models, image, ...).
    Кэшируется: используется и роутером, и реестром адаптеров, и генератором
    изображений."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class RouteDecision:
    model: str
    web_search: bool
    require_human_review: bool
    used_fallback_confidence: bool  # True, если сработал fallback по низкой уверенности
    max_tokens: int = 2048  # ДОБАВЛЕНО: раньше max_tokens нигде не передавался через RouteDecision


class Router:
    """Роутер поверх конфига task_type -> модель. Загружает YAML один раз
    при старте; для правки без релиза можно позже заменить источник на БД,
    не трогая интерфейс decide()."""

    def __init__(self, config_path: Path = _CONFIG_PATH):
        self._config = load_config(config_path)

    @property
    def default_max_tokens(self) -> int:
        return self._config.get("default_max_tokens", 2048)

    def decide(self, task_type: str, confidence: float) -> RouteDecision:
        threshold = self._config["confidence_threshold"]

        if confidence < threshold:
            return RouteDecision(
                model=self._config["fallback_model"],
                web_search=False,
                require_human_review=False,
                used_fallback_confidence=True,
                max_tokens=self.default_max_tokens,
            )

        rule = self._config["routing_rules"].get(task_type)
        if rule is None:
            # Неизвестный (но прошедший классификатор) task_type — не должно
            # происходить, т.к. classifier.py уже фильтрует по KNOWN_TASK_TYPES,
            # но не полагаемся на это молча.
            return RouteDecision(
                model=self._config["fallback_model"],
                web_search=False,
                require_human_review=False,
                used_fallback_confidence=True,
                max_tokens=self.default_max_tokens,
            )

        return RouteDecision(
            model=rule["primary"],
            web_search=rule.get("web_search", False),
            require_human_review=rule.get("require_human_review", False),
            used_fallback_confidence=False,
            max_tokens=rule.get("max_tokens", self.default_max_tokens),
        )

    def rule_for(self, task_type: str) -> dict:
        """Флаги (web_search, require_human_review, max_tokens) для task_type,
        без выбора модели. Нужен, когда модель выбирает не роутер, а сам
        пользователь (см. ChatRequest.model в chat.py) — флаги задачи при
        этом всё равно должны применяться (например, включить web_search
        или дать больше max_tokens для выбранной пользователем модели, если
        задача классифицирована соответствующим образом).
        """
        return self._config["routing_rules"].get(task_type, {})
