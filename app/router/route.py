from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yaml"

THINKING_LEVELS = ("off", "low", "medium", "high", "max")
PROVIDERS = ("openai", "anthropic", "gemini")
THINKING_MODES = ("adaptive", "budget", "level", "effort", "none")
THINKING_OFF_MODES = ("disabled", "between_tools", "unsupported")
FORCED_TOOL_MODES = ("with_thinking", "no_thinking", "unsupported")


@lru_cache
def load_config(path: Path = _CONFIG_PATH) -> dict:
    """YAML-конфиг роутера целиком (models, routing_rules, image, ...).
    Кэшируется: используется роутером, реестром адаптеров и генератором изображений."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize_thinking(value) -> str:
    """YAML 1.1 читает голое `off` как False — приводим к строке."""
    if value is None or value is False:
        return "off"
    return str(value).lower()


def validate_config(config: dict, known_tools: Iterable[str]) -> None:
    """Проверяет config.yaml; при ошибках бросает ValueError со списком всех проблем.
    Вызывается при построении адаптеров (старт приложения) и в тестах."""
    tools = set(known_tools)
    errors: list[str] = []
    models = config.get("models") or {}

    for name, spec in models.items():
        if spec.get("provider") not in PROVIDERS:
            errors.append(f"models.{name}: provider должен быть одним из {PROVIDERS}")
        if not isinstance(spec.get("id"), str) or not spec["id"]:
            errors.append(f"models.{name}: не задан id")
        caps = spec.get("caps") or {}
        if caps.get("thinking_mode") not in THINKING_MODES:
            errors.append(f"models.{name}.caps.thinking_mode: допустимо {THINKING_MODES}")
        if caps.get("forced_tool") not in FORCED_TOOL_MODES:
            errors.append(f"models.{name}.caps.forced_tool: допустимо {FORCED_TOOL_MODES}")
        if (
            caps.get("thinking_mode") == "adaptive"
            and caps.get("thinking_off") not in THINKING_OFF_MODES
        ):
            errors.append(f"models.{name}.caps.thinking_off: допустимо {THINKING_OFF_MODES}")
        if caps.get("thinking_mode") == "level" and not caps.get("thinking_levels"):
            errors.append(f"models.{name}.caps.thinking_levels: нужен непустой список")
        if not isinstance(caps.get("max_output_tokens"), int):
            errors.append(f"models.{name}.caps.max_output_tokens: нужно целое число")

    def check_model_ref(where: str, name, *, allow_explicit_only: bool = False) -> None:
        if name not in models:
            errors.append(f"{where}: модель {name!r} не описана в models")
        elif models[name].get("explicit_only") and not allow_explicit_only:
            errors.append(f"{where}: {name!r} — explicit_only, роутер не может её выбирать")

    check_model_ref("classifier.model", (config.get("classifier") or {}).get("model"))
    check_model_ref("low_confidence.model", (config.get("low_confidence") or {}).get("model"))
    if (
        normalize_thinking((config.get("low_confidence") or {}).get("thinking"))
        not in THINKING_LEVELS
    ):
        errors.append("low_confidence.thinking: недопустимое значение")

    for task, rule in (config.get("routing_rules") or {}).items():
        where = f"routing_rules.{task}"
        check_model_ref(f"{where}.model", rule.get("model"))
        if normalize_thinking(rule.get("thinking")) not in THINKING_LEVELS:
            errors.append(f"{where}.thinking: допустимо {THINKING_LEVELS}")
        if not isinstance(rule.get("max_tokens"), int) or rule["max_tokens"] <= 0:
            errors.append(f"{where}.max_tokens: нужно положительное целое")
        if rule.get("forced_tool") is not None and rule["forced_tool"] not in tools:
            errors.append(f"{where}.forced_tool: неизвестный инструмент {rule['forced_tool']!r}")

    for name in config.get("prices") or {}:
        if name not in models:
            errors.append(f"prices.{name}: нет такой модели в models")

    deck = config.get("deck") or {}
    hard_cap = 8  # app.limits.DECK_MAX_IMAGES_HARD (route.py — листовой модуль, без импорта limits)
    if not isinstance(deck.get("max_images"), int) or not 0 <= deck["max_images"] <= hard_cap:
        errors.append(f"deck.max_images: целое от 0 до {hard_cap}")
    for key in ("hero_quality", "content_quality"):
        if deck.get(key) not in ("low", "medium", "high", "auto"):
            errors.append(f"deck.{key}: low | medium | high | auto")
    for key in ("hero_size", "content_size", "wide_size"):
        if not deck.get(key):
            errors.append(f"deck.{key}: не задано")
    if not isinstance(deck.get("images_timeout_s"), (int, float)) or deck["images_timeout_s"] <= 0:
        errors.append("deck.images_timeout_s: положительное число")

    image = config.get("image") or {}
    for key in ("model", "fallback_model", "quality"):
        if not image.get(key):
            errors.append(f"image.{key}: не задано")

    if errors:
        raise ValueError("Некорректный router/config.yaml:\n  - " + "\n  - ".join(errors))


@dataclass
class RouteDecision:
    model: str
    web_search: bool
    require_human_review: bool
    used_fallback_confidence: bool  # True, если сработал fallback по низкой уверенности
    max_tokens: int = 8192
    thinking: str = "off"
    forced_tool: str | None = None
    timeout_s: int = 600


class Router:
    """Роутер поверх конфига task_type -> модель. Все параметры (модель, max_tokens,
    thinking, принудительный tool, таймаут) берутся из routing_rules."""

    def __init__(self, config_path: Path = _CONFIG_PATH):
        self._config = load_config(config_path)

    @property
    def default_max_tokens(self) -> int:
        return self._config.get("default_max_tokens", 8192)

    @property
    def threshold(self) -> float:
        return self._config["confidence_threshold"]

    def _clamp_tokens(self, model: str, max_tokens: int) -> int:
        cap = self._config["models"][model]["caps"]["max_output_tokens"]
        return min(max_tokens, cap)

    def _from_rule(self, rule: dict, model: str, *, used_fallback: bool) -> RouteDecision:
        return RouteDecision(
            model=model,
            web_search=rule.get("web_search", False),
            require_human_review=rule.get("require_human_review", False),
            used_fallback_confidence=used_fallback,
            max_tokens=self._clamp_tokens(model, rule.get("max_tokens", self.default_max_tokens)),
            thinking=normalize_thinking(rule.get("thinking")),
            forced_tool=rule.get("forced_tool"),
            timeout_s=rule.get("timeout_s", self._config["limits"]["provider_timeout_s"]),
        )

    def _low_confidence(self, model: str | None = None) -> RouteDecision:
        rule = {**self._config["low_confidence"]}
        rule.pop("forced_tool", None)  # при низкой уверенности файлы не принуждаем
        return self._from_rule(rule, model or rule["model"], used_fallback=True)

    def decide(self, task_type: str, confidence: float) -> RouteDecision:
        """Авто-роутинг. Низкая уверенность или неизвестная категория -> сильная
        модель по умолчанию (low_confidence), никогда не flash."""
        rule = self._config["routing_rules"].get(task_type)
        if confidence < self.threshold or rule is None:
            return self._low_confidence()
        return self._from_rule(rule, rule["model"], used_fallback=False)

    def decide_explicit(self, task_type: str, confidence: float, model: str) -> RouteDecision:
        """Пользователь выбрал модель сам: модель его, а флаги категории
        (web_search, needs_review, принудительный tool, thinking, лимиты) применяются."""
        rule = self._config["routing_rules"].get(task_type)
        if confidence < self.threshold or rule is None:
            return self._low_confidence(model)
        return self._from_rule(rule, model, used_fallback=False)

    def rule_for(self, task_type: str) -> dict:
        """Правило категории как есть (для проверок и тестов)."""
        return self._config["routing_rules"].get(task_type, {})
