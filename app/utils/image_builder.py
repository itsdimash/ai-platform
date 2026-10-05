import asyncio
import base64
import os
from pathlib import Path

from openai import AsyncOpenAI

from app.utils.r2 import upload_file_to_r2

IMAGE_TOOL = {
    "name": "generate_image",
    "description": "Generates a photo, illustration, or image using OpenAI Image API and returns a permanent URL when requested by the user.",
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Detailed English prompt describing the image to generate.",
            },
            "size": {
                "type": "string",
                # ИСПРАВЛЕНО: 1024x1792 / 1792x1024 — размеры старого DALL-E 3,
                # gpt-image-2 их не поддерживает. Актуальные стандартные
                # размеры (плюс "auto") см. в доке OpenAI на images.generate.
                "enum": ["1024x1024", "1536x1024", "1024x1536", "auto"],
                "default": "1024x1024",
                "description": "Image resolution aspect ratio.",
            },
        },
        "required": ["prompt"],
    },
}


def _get_openai_api_key() -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        return api_key

    try:
        from dotenv import load_dotenv

        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        if api_key:
            return api_key
    except ImportError:
        pass

    root_dir = Path(__file__).resolve().parent.parent.parent
    env_file = root_dir / ".env"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("OPENAI_API_KEY"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        val = parts[1].strip().strip('"').strip("'")
                        if val:
                            return val

    try:
        import app.config as cfg

        for attr in ("OPENAI_API_KEY", "openai_api_key"):
            if hasattr(cfg, attr):
                return getattr(cfg, attr)
            if hasattr(cfg, "settings") and hasattr(cfg.settings, attr):
                return getattr(cfg.settings, attr)
            if hasattr(cfg, "config") and hasattr(cfg.config, attr):
                return getattr(cfg.config, attr)
    except Exception:
        pass

    raise ValueError("OPENAI_API_KEY не найден в файле .env или окружении.")


async def generate_and_save_image(prompt: str, size: str = "1024x1024") -> str:
    try:
        api_key = _get_openai_api_key()
        client = AsyncOpenAI(api_key=api_key)

        response = await asyncio.wait_for(
            client.images.generate(
                # ИСПРАВЛЕНО: "gpt-image-2.5-sunburst" не существует как модель
                # OpenAI — вызов падал на каждом обращении (invalid model).
                # Актуальная флагманская модель на апрель 2026 — "gpt-image-2".
                model="gpt-image-2",
                prompt=prompt,
                size=size,
                # ИСПРАВЛЕНО: quality="standard" был валиден для старого
                # DALL-E API, но gpt-image-2 принимает только
                # low / medium / high / auto — "standard" даёт 400 invalid_value.
                quality="auto",
                n=1,
            ),
            timeout=45.0,
        )

        temp_url = response.data[0].url
        image_b64 = response.data[0].b64_json

        if image_b64:
            # ИСПРАВЛЕНО: gpt-image-2 (и вся линейка gpt-image-*) НЕ
            # возвращает url — по документации OpenAI response_format для
            # GPT image моделей не поддерживается, они всегда отдают
            # base64. Раньше код всегда ждал .url и падал с "API не вернул
            # URL изображения" на каждом успешном по сути вызове.
            image_bytes = base64.b64decode(image_b64)
        elif temp_url:
            # На случай отката на dall-e-3/dall-e-2 (они всё ещё могут
            # вернуть url) — оставляем путь скачивания как запасной.
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as http_client:
                img_res = await http_client.get(temp_url)
                img_res.raise_for_status()
                image_bytes = img_res.content
        else:
            raise ValueError("API не вернул ни url, ни b64_json изображения.")

        return upload_file_to_r2(
            file_bytes=image_bytes,
            original_filename="generated_image.png",
            content_type="image/png",
            folder="images",
        )
    except Exception as err:
        print(f"[IMAGE GENERATION ERROR] {err}")
        raise RuntimeError(f"Ошибка при генерации изображения: {err!s}") from err
