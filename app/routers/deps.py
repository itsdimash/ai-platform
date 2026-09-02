from ..adapters.base import ModelAdapter
from ..adapters.registry import build_adapters
from ..config import get_settings

_adapters_cache: dict[str, ModelAdapter] | None = None


def get_adapters() -> dict[str, ModelAdapter]:
    """Пул адаптеров строится один раз при первом запросе и переиспользуется —
    не создаём новый HTTP-клиент на каждый вызов."""
    global _adapters_cache
    if _adapters_cache is None:
        _adapters_cache = build_adapters(get_settings())
    return _adapters_cache
