import asyncio
import base64

import httpx
from openai import AsyncOpenAI

from app.config import get_settings
from app.router.route import load_config

IMAGE_TOOL = {
    "name": "generate_image",
    "description": "Generates a photo, illustration, or image using OpenAI Image API and delivers it to the user as an attached image file.",
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Detailed English prompt describing the image to generate.",
            },
            "size": {
                "type": "string",
                # 1024x1792 / 1792x1024 — размеры старого DALL-E 3, GPT image
                # моделями не поддерживаются.
                "enum": ["1024x1024", "1536x1024", "1024x1536", "auto"],
                "default": "1024x1024",
                "description": "Image resolution aspect ratio.",
            },
        },
        "required": ["prompt"],
    },
}


async def generate_image_bytes(prompt: str, size: str = "1024x1024") -> bytes:
    """Генерирует изображение и возвращает PNG-байты (загрузка в R2 — в app/tools).
    Любая ошибка (ключ, модель, таймаут) пробрасывается наверх — её превращает
    в ToolExecutionError общий исполнитель инструментов."""
    api_key = get_settings().openai_api_key
    if not api_key:
        raise ValueError("OPENAI_API_KEY не задан")

    image_cfg = load_config().get("image", {})
    model = image_cfg.get("model", "gpt-image-2")
    timeout_s = float(image_cfg.get("timeout_s", 45))

    client = AsyncOpenAI(api_key=api_key)
    response = await asyncio.wait_for(
        client.images.generate(
            model=model,
            prompt=prompt,
            size=size,
            # GPT image модели принимают только low / medium / high / auto.
            quality="auto",
            n=1,
        ),
        timeout=timeout_s,
    )

    temp_url = response.data[0].url
    image_b64 = response.data[0].b64_json

    if image_b64:
        # GPT image модели всегда отдают base64, а не url.
        image_bytes = base64.b64decode(image_b64)
    elif temp_url:
        # Запасной путь на случай dall-e-*: они могут вернуть временный url.
        async with httpx.AsyncClient(timeout=30.0) as http_client:
            img_res = await http_client.get(temp_url)
            img_res.raise_for_status()
            image_bytes = img_res.content
    else:
        raise ValueError("API не вернул ни url, ни b64_json изображения.")

    return image_bytes
