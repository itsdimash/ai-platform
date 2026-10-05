import asyncio
import base64
import logging

import httpx
import openai
from openai import AsyncOpenAI

from app.config import get_settings
from app.router.route import load_config

logger = logging.getLogger(__name__)

IMAGE_TOOL = {
    "name": "generate_image",
    "description": (
        "Generates an image (photo, illustration, logo, poster) and delivers it to the user "
        "as an attached image file. Use it as soon as the user asks to draw, generate or "
        "create a picture — do not ask clarifying questions, make reasonable assumptions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Rich, specific English prompt for the image model. Always include: the "
                    "subject and its action, the setting, the visual style/medium (photo, "
                    "watercolor, 3D render, flat vector...), lighting, composition and camera "
                    "angle, mood and colour palette. 40-120 words. No text to be rendered "
                    "unless the user asked for it."
                ),
            },
            "size": {
                "type": "string",
                # 1024x1792 / 1792x1024 — размеры старого DALL-E 3, GPT image
                # моделями не поддерживаются.
                "enum": ["1024x1024", "1536x1024", "1024x1536", "auto"],
                "description": "Image size: square, landscape (1536x1024) or portrait (1024x1536).",
            },
            "caption": {
                "type": "string",
                "description": (
                    "One short sentence in the user's language describing what was created "
                    "(shown to the user as the reply). No markdown."
                ),
            },
        },
        "required": ["prompt", "caption"],
    },
}

# Ошибки, при которых имеет смысл попробовать запасную модель картинок.
_FALLBACK_ERRORS = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
    asyncio.TimeoutError,
)


async def _generate_once(
    client: AsyncOpenAI, model: str, prompt: str, size: str, quality: str, timeout_s: float
) -> bytes:
    response = await asyncio.wait_for(
        client.images.generate(model=model, prompt=prompt, size=size, quality=quality, n=1),
        timeout=timeout_s,
    )
    image_b64 = response.data[0].b64_json
    temp_url = response.data[0].url
    if image_b64:
        # GPT image модели всегда отдают base64, а не url.
        return base64.b64decode(image_b64)
    if temp_url:
        # Запасной путь на случай dall-e-*: они могут вернуть временный url.
        async with httpx.AsyncClient(timeout=30.0) as http_client:
            img_res = await http_client.get(temp_url)
            img_res.raise_for_status()
            return img_res.content
    raise ValueError("API не вернул ни url, ни b64_json изображения.")


async def generate_image_bytes(
    prompt: str, size: str | None = None, quality: str | None = None
) -> tuple[bytes, str]:
    """Генерирует изображение, возвращает (PNG-байты, реальный id использованной модели).

    Модель, качество, размер по умолчанию и таймаут — из config.yaml -> image. При 5xx,
    таймауте или лимите основной модели однократно пробуется fallback_model; фактически
    использованная модель возвращается наверх (в API это image_model_used). Любая ошибка
    пробрасывается — её превращает в ToolExecutionError общий исполнитель инструментов."""
    api_key = get_settings().openai_api_key
    if not api_key:
        raise ValueError("OPENAI_API_KEY не задан")

    cfg = load_config()["image"]
    size = size or cfg.get("size_default", "1024x1024")
    quality = quality or cfg["quality"]
    timeout_s = float(cfg.get("timeout_s", 300))
    # Ретраи 429/5xx/таймаутов с backoff — встроенный механизм SDK.
    client = AsyncOpenAI(api_key=api_key, timeout=timeout_s, max_retries=cfg.get("retries", 2))

    primary, fallback = cfg["model"], cfg.get("fallback_model")
    try:
        return await _generate_once(client, primary, prompt, size, quality, timeout_s), primary
    except _FALLBACK_ERRORS as exc:
        if not fallback or fallback == primary:
            raise
        logger.warning(
            "image model %s failed (%s), trying %s", primary, type(exc).__name__, fallback
        )
        return await _generate_once(client, fallback, prompt, size, quality, timeout_s), fallback
