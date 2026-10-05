"""Фасад генерации презентаций. Реализация — пакет app/utils/deck:
schema (tool + нормализация) -> layout (разбиение/подбор кегля) -> images -> render."""

from collections import Counter

from app.utils.deck.layout import plan_slides
from app.utils.deck.render import RenderResult, render_deck
from app.utils.deck.schema import PRESENTATION_TOOL, Deck, Slide, normalize

# Накопительные счётчики нормализации/разбиений за жизнь процесса (для отчётов и диагностики).
STATS_TOTAL: Counter = Counter()


def prepare_deck(args: dict) -> tuple[Deck, list[Slide]]:
    """Ответ модели -> нормализованная колода + раскладка слайдов (разбиения, лимиты)."""
    deck = normalize(args)
    slides = plan_slides(deck)
    return deck, slides


def render(deck: Deck, slides: list[Slide], images: dict) -> RenderResult:
    result = render_deck(deck, slides, images)
    STATS_TOTAL.update(deck.stats)
    return result


def build_presentation(title: str, subtitle: str, slides_data: list) -> bytes:
    """Совместимый вызов (title, subtitle, [{title, content}]) -> .pptx без картинок."""
    args = {
        "title": title,
        "subtitle": subtitle,
        "theme": "graphite",
        "slides": [
            {"layout": "bullets", "title": s.get("title", ""), "bullets": s.get("content", [])}
            for s in slides_data
        ],
    }
    deck, slides = prepare_deck(args)
    return render(deck, slides, {}).data


__all__ = ["PRESENTATION_TOOL", "STATS_TOTAL", "build_presentation", "prepare_deck", "render"]
