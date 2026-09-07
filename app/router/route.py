from dataclasses import dataclass
from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


@dataclass
class RouteDecision:
    model: str
    web_search: bool
    require_human_review: bool
    used_fallback_confidence: bool  # True, если сработал fallback по низкой уверенности


class Router:
    """Роутер поверх конфига task_type -> модель. Загружает YAML один раз
    при старте; для правки без релиза можно позже заменить источник на БД,
    не трогая интерфейс decide()."""

    def __init__(self, config_path: Path = _CONFIG_PATH):
        with open(config_path, encoding="utf-8") as f:
            self._config = yaml.safe_load(f)

    def decide(self, task_type: str, confidence: float) -> RouteDecision:
        threshold = self._config["confidence_threshold"]

        if confidence < threshold:
            return RouteDecision(
                model=self._config["fallback_model"],
                web_search=False,
                require_human_review=False,
                used_fallback_confidence=True,
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
            )

        return RouteDecision(
            model=rule["primary"],
            web_search=rule.get("web_search", False),
            require_human_review=rule.get("require_human_review", False),
            used_fallback_confidence=False,
        )

    def rule_for(self, task_type: str) -> dict:
        """Флаги (web_search, require_human_review) для task_type, без выбора
        модели. Нужен, когда модель выбирает не роутер, а сам пользователь
        (см. ChatRequest.model в chat.py) — флаги задачи при этом всё равно
        должны применяться (например, включить web_search для выбранной
        пользователем модели, если задача классифицирована как web_search).
        """
        return self._config["routing_rules"].get(task_type, {})
