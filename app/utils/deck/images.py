"""Картинки презентации: параллельная генерация с лимитом одновременных запросов и общим
таймаутом (config.yaml -> deck). Картинка, которая не успела или упала, просто пропускается —
слайд рисуется без неё (акцентная полоса вместо картинки). Титульная (hero) использует
качество hero_quality, остальные — content_quality (дешевле)."""

import asyncio
import logging

from app.limits import DECK_MAX_IMAGES_HARD
from app.router.route import load_config
from app.utils.deck.schema import IMAGE_LAYOUTS, Deck, Slide
from app.utils.image_builder import generate_image_bytes

logger = logging.getLogger(__name__)

# Запреты добавляются к КАЖДОМУ промпту независимо от того, что написала модель.
PROMPT_RULES = (
    " No text, lettering, captions or watermarks; no logos or real brands; "
    "no recognizable real people."
)


def plan_image_jobs(
    deck: Deck, slides: list[Slide], cfg: dict
) -> list[tuple[object, str, str, str]]:
    """[(ключ, prompt, размер, качество)] в порядке приоритета: hero, затем слайды по порядку,
    не больше deck.max_images (и не больше жёсткого потолка)."""
    jobs: list[tuple[object, str, str, str]] = []
    if deck.hero_prompt:
        jobs.append(("hero", deck.hero_prompt, cfg["hero_size"], cfg["hero_quality"]))
    for s in slides:
        if s.part == 1 and s.image_prompt and s.layout in IMAGE_LAYOUTS and s.layout != "title":
            size = cfg["wide_size"] if s.layout == "image" else cfg["content_size"]
            jobs.append((s.uid, s.image_prompt, size, cfg["content_quality"]))
    limit = min(int(cfg["max_images"]), DECK_MAX_IMAGES_HARD)
    if len(jobs) > limit:
        dropped = len(jobs) - limit
        deck.stats["images_dropped"] += dropped
        logger.warning("deck images: images_dropped %s (лимит %s)", dropped, limit)
        jobs = jobs[:limit]
    return jobs


async def generate_deck_images(deck: Deck, slides: list[Slide]) -> tuple[dict, str | None]:
    """({ключ: байты}, реальный id модели картинок). Ключ: "hero" или uid слайда."""
    cfg = load_config()["deck"]
    jobs = plan_image_jobs(deck, slides, cfg)
    if not jobs:
        return {}, None

    sem = asyncio.Semaphore(int(cfg["concurrency"]))

    async def one(key: object, prompt: str, size: str, quality: str):
        async with sem:
            data, model = await generate_image_bytes(
                prompt + PROMPT_RULES, size=size, quality=quality
            )
            return key, data, model

    tasks = [asyncio.create_task(one(*job)) for job in jobs]
    done, pending = await asyncio.wait(tasks, timeout=float(cfg["images_timeout_s"]))
    for task in pending:
        task.cancel()
    if pending:
        deck.stats["image_timeout"] += len(pending)
        logger.warning("deck images: image_timeout %s из %s", len(pending), len(tasks))

    images: dict = {}
    model_used: str | None = None
    for task in done:
        try:
            key, data, model = task.result()
        except Exception as exc:  # noqa: BLE001 — сбой одной картинки не должен ронять колоду
            deck.stats["image_failed"] += 1
            logger.warning("deck images: image_failed (%s: %s)", type(exc).__name__, str(exc)[:120])
            continue
        images[key] = data
        if model_used is None or key == "hero":
            model_used = model
    deck.stats["images_generated"] += len(images)
    return images, model_used
