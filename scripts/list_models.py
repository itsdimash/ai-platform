"""Список моделей, реально доступных по вашим ключам, + сверка с router/config.yaml.

Запуск из корня репозитория:
    uv run python scripts/list_models.py            # все провайдеры
    uv run python scripts/list_models.py anthropic  # только один

Ключи берутся из Settings (.env / окружение) и НИКОГДА не печатаются.
Скрипт только читает (endpoints "list models"), ничего не создаёт и не тратит токены.

Что делает:
1. Печатает ID моделей по каждому провайдеру (Gemini — с поддерживаемыми методами,
   чтобы отличить текстовые модели от image-моделей).
2. Сверяет ID из config.yaml (секции `models` и `image`) со списком: NOT LISTED =
   ключ такой модели не видит / ID неверный.
3. Помечает подозрительные по возрасту ID (SUSPECT_PATTERNS). API провайдеров не
   отдают статус deprecation, поэтому это эвристика — сверяйте с документацией.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings

CONFIG_PATH = Path(__file__).resolve().parent.parent / "app" / "router" / "config.yaml"

# ID/подстроки, которые аудит счёл потенциально устаревшими или непроверенными.
SUSPECT_PATTERNS = {
    "gpt-4o": "модель прошлого поколения (2024) — проверьте, нет ли более новой",
    "claude-sonnet-5": "проверьте точный ID: актуальный может быть claude-sonnet-5-5",
    "claude-opus-5": "проверьте точный ID: актуальный может быть claude-opus-5-5",
    "gemini-3.6-flash": "не проверено — сверьте со списком Gemini ниже",
    "gpt-image-2": "не проверено — сверьте со списком OpenAI ниже",
}


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def list_anthropic(api_key: str) -> set[str]:
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    ids: set[str] = set()
    for m in client.models.list(limit=100):
        ids.add(m.id)
        print(f"  {m.id:<40} {getattr(m, 'display_name', '')}")
    return ids


def list_openai(api_key: str) -> set[str]:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    ids = {m.id for m in client.models.list()}
    # Оставляем только то, что относится к чату/изображениям — полный список
    # содержит embeddings, whisper, tts и т.д.
    interesting = sorted(
        i for i in ids if i.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-")) and "audio" not in i
    )
    for i in interesting:
        tag = "  [image]" if "image" in i else ""
        print(f"  {i}{tag}")
    print(f"  (всего моделей в ответе: {len(ids)}; показаны gpt-*/o*)")
    return ids


def list_gemini(api_key: str) -> set[str]:
    from google import genai

    client = genai.Client(api_key=api_key)
    ids: set[str] = set()
    for m in client.models.list():
        name = (m.name or "").removeprefix("models/")
        ids.add(name)
        actions = ",".join(getattr(m, "supported_actions", None) or [])
        print(f"  {name:<45} {actions}")
    return ids


def check_config(listed: dict[str, set[str]]) -> None:
    _section("Сверка с app/router/config.yaml")
    if not CONFIG_PATH.exists():
        print("  config.yaml не найден")
        return
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    entries: list[tuple[str, str, str]] = []  # (label, provider, model_id)
    for logical, spec in (config.get("models") or {}).items():
        entries.append((logical, spec["provider"], spec["id"]))
    image = config.get("image") or {}
    if image.get("model"):
        entries.append(("image", image.get("provider", "openai"), image["model"]))

    if not entries:
        print("  В config.yaml нет секции `models` — сверять нечего.")
        return

    for label, provider, model_id in entries:
        pool = listed.get(provider)
        if pool is None:
            status = "SKIPPED (провайдер не опрашивался / нет ключа)"
        elif model_id in pool:
            status = "OK"
        else:
            status = "NOT LISTED — ключ эту модель не видит или ID неверный"
        notes = [
            note
            for pattern, note in SUSPECT_PATTERNS.items()
            if model_id == pattern or model_id.startswith(pattern + "-")
        ]
        suffix = f"   ⚠ {'; '.join(notes)}" if notes else ""
        print(f"  {label:<16} {provider:<10} {model_id:<40} {status}{suffix}")

    tool = config.get("anthropic_web_search_tool")
    if tool:
        print(f"\n  anthropic_web_search_tool = {tool}  (проверьте по документации Anthropic)")


def main() -> None:
    wanted = set(sys.argv[1:]) or {"anthropic", "openai", "gemini"}
    settings = get_settings()
    providers = {
        "anthropic": (settings.anthropic_api_key, list_anthropic),
        "openai": (settings.openai_api_key, list_openai),
        "gemini": (settings.gemini_api_key, list_gemini),
    }

    listed: dict[str, set[str]] = {}
    for name, (key, fn) in providers.items():
        if name not in wanted:
            continue
        _section(name.upper())
        if not key:
            print(f"  Ключ {name.upper()}_API_KEY не задан — пропуск.")
            continue
        try:
            listed[name] = fn(key)
        except Exception as exc:  # noqa: BLE001 — скрипт диагностики, печатаем причину и идём дальше
            # Тип ошибки + сообщение; ключ SDK в сообщения не включает.
            print(f"  Ошибка запроса списка моделей: {type(exc).__name__}: {str(exc)[:300]}")

    check_config(listed)


if __name__ == "__main__":
    main()
