from ..config import Settings
from .anthropic_adapter import AnthropicAdapter
from .base import ModelAdapter
from .gemini_adapter import GeminiAdapter
from .openai_adapter import OpenAIAdapter

# Логическое имя (используется в router/config.yaml) -> как создать адаптер.
# Логическое имя не обязано совпадать 1:1 с версией модели у провайдера —
# это даёт возможность поменять реальную модель под капотом одной строкой,
# не трогая routing_rules.
MODEL_FACTORY = {
    "gpt-4o-mini": lambda s: OpenAIAdapter(s.openai_api_key, "gpt-4o-mini"),
    "gpt-4o": lambda s: OpenAIAdapter(s.openai_api_key, "gpt-4o"),
    "claude-haiku": lambda s: AnthropicAdapter(s.anthropic_api_key, "claude-haiku-4-5-20251001"),
    "claude-sonnet": lambda s: AnthropicAdapter(s.anthropic_api_key, "claude-sonnet-5"),
    "claude-opus": lambda s: AnthropicAdapter(s.anthropic_api_key, "claude-opus-5"),
    "gemini-flash": lambda s: GeminiAdapter(s.gemini_api_key, "gemini-3.6-flash"),
    "gemini-pro": lambda s: GeminiAdapter(s.gemini_api_key, "gemini-3.1-pro-preview"),
}


def build_adapters(settings: Settings) -> dict[str, ModelAdapter]:
    """Строит пул адаптеров один раз при старте приложения."""
    return {name: factory(settings) for name, factory in MODEL_FACTORY.items()}
