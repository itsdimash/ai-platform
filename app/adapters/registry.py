from ..config import Settings
from ..router.route import load_config
from .anthropic_adapter import AnthropicAdapter
from .base import ModelAdapter
from .gemini_adapter import GeminiAdapter
from .openai_adapter import OpenAIAdapter


def _build_openai(settings: Settings, model_id: str, config: dict) -> ModelAdapter:
    return OpenAIAdapter(api_key=settings.openai_api_key, model=model_id)


def _build_anthropic(settings: Settings, model_id: str, config: dict) -> ModelAdapter:
    return AnthropicAdapter(
        api_key=settings.anthropic_api_key,
        model=model_id,
        web_search_tool_type=config.get("anthropic_web_search_tool", "web_search_20250305"),
    )


def _build_gemini(settings: Settings, model_id: str, config: dict) -> ModelAdapter:
    return GeminiAdapter(api_key=settings.gemini_api_key, model=model_id)


_PROVIDER_BUILDERS = {
    "openai": _build_openai,
    "anthropic": _build_anthropic,
    "gemini": _build_gemini,
}


def build_adapters(settings: Settings) -> dict[str, ModelAdapter]:
    """Строит пул адаптеров один раз при старте приложения.

    Логическое имя (ключ в config.yaml -> models; его же используют
    routing_rules и параметр API `model`) -> адаптер с РЕАЛЬНЫМ ID модели
    провайдера. Все аргументы передаются по имени: раньше вторым позиционным
    аргументом шёл ID модели, который попадал в `openai_api_key`, а модель
    молча бралась из дефолта конструктора.
    """
    config = load_config()
    adapters: dict[str, ModelAdapter] = {}
    for logical_name, spec in config["models"].items():
        provider = spec["provider"]
        builder = _PROVIDER_BUILDERS.get(provider)
        if builder is None:
            raise ValueError(
                f"config.yaml: неизвестный provider {provider!r} у модели {logical_name!r}"
            )
        adapters[logical_name] = builder(settings, spec["id"], config)
    return adapters
