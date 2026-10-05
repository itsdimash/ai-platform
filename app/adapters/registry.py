from ..config import Settings
from ..router.route import load_config, validate_config
from ..tools import ALL_TOOLS
from .anthropic_adapter import AnthropicAdapter
from .base import ModelAdapter, ModelCaps
from .gemini_adapter import GeminiAdapter
from .openai_adapter import OpenAIAdapter


def _build_openai(settings: Settings, model_id: str, caps: ModelCaps, config: dict) -> ModelAdapter:
    limits = config["limits"]
    return OpenAIAdapter(
        api_key=settings.openai_api_key,
        model=model_id,
        caps=caps,
        timeout_s=limits["provider_timeout_s"],
        max_retries=limits["retries"],
    )


def _build_anthropic(
    settings: Settings, model_id: str, caps: ModelCaps, config: dict
) -> ModelAdapter:
    limits = config["limits"]
    return AnthropicAdapter(
        api_key=settings.anthropic_api_key,
        model=model_id,
        caps=caps,
        web_search_tool_type=config.get("anthropic_web_search_tool", "web_search_20250305"),
        timeout_s=limits["provider_timeout_s"],
        max_retries=limits["retries"],
    )


def _build_gemini(settings: Settings, model_id: str, caps: ModelCaps, config: dict) -> ModelAdapter:
    limits = config["limits"]
    return GeminiAdapter(
        api_key=settings.gemini_api_key,
        model=model_id,
        caps=caps,
        timeout_s=limits["provider_timeout_s"],
        max_retries=limits["retries"],
    )


_PROVIDER_BUILDERS = {
    "openai": _build_openai,
    "anthropic": _build_anthropic,
    "gemini": _build_gemini,
}


def build_adapters(settings: Settings) -> dict[str, ModelAdapter]:
    """Строит пул адаптеров один раз при старте приложения.

    Логическое имя (ключ в config.yaml -> models; его же используют routing_rules и
    параметр API `model`) -> адаптер с РЕАЛЬНЫМ ID модели провайдера и её проверенными
    возможностями. Конфиг валидируется при построении: ошибка в config.yaml роняет
    старт, а не первый пользовательский запрос.
    """
    config = load_config()
    validate_config(config, {t["name"] for t in ALL_TOOLS})
    adapters: dict[str, ModelAdapter] = {}
    for logical_name, spec in config["models"].items():
        builder = _PROVIDER_BUILDERS[spec["provider"]]
        caps = ModelCaps.from_dict(spec.get("caps", {}))
        adapters[logical_name] = builder(settings, spec["id"], caps, config)
    return adapters
