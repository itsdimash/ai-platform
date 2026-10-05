"""Подготовка вложений под лимиты провайдера: уменьшение изображений (Pillow) и
решение «нативно или текстом» для PDF.

Оригиналы НЕ изменяются: в R2 сохраняются исходные байты, а провайдеру уходит
уменьшенная копия. Функции синхронные (Pillow/CPU) — вызывать через asyncio.to_thread.
"""

import io
from dataclasses import dataclass, field

from fastapi import HTTPException, status
from PIL import Image, ImageOps, UnidentifiedImageError

from app import limits
from app.utils.uploads import ParsedFile

# Защита от «бомб декомпрессии»: больше этого числа пикселей Pillow откажется открывать.
Image.MAX_IMAGE_PIXELS = 100_000_000

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
PROVIDER_GEMINI = "gemini"


def _request_budget(provider: str) -> int:
    """Бюджет «сырых» байт вложений на запрос (читается из limits в момент вызова)."""
    if provider == PROVIDER_ANTHROPIC:
        return limits.ANTHROPIC_REQUEST_RAW_BUDGET
    if provider == PROVIDER_GEMINI:
        return limits.GEMINI_REQUEST_RAW_BUDGET
    return limits.OPENAI_REQUEST_RAW_BUDGET


def _bad_image(name: str) -> HTTPException:
    return HTTPException(
        status.HTTP_400_BAD_REQUEST, detail=f"Не удалось обработать изображение {name!r}"
    )


def image_size(data: bytes, name: str = "image") -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise _bad_image(name) from exc


def shrink_image(
    data: bytes, *, max_edge: int, max_bytes: int | None = None, name: str = "image"
) -> tuple[bytes, str]:
    """JPEG-копия с длинной стороной <= max_edge (и размером <= max_bytes, если
    задан). Прозрачность заливается белым, EXIF-поворот применяется."""
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode in ("RGBA", "LA", "P"):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")

            edges = [e for e in (max_edge, *limits.IMAGE_SHRINK_LADDER) if e <= max_edge]
            best = b""
            for edge in sorted(set(edges), reverse=True):
                candidate = image.copy()
                candidate.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                for quality in (88, 80, 70):
                    buf = io.BytesIO()
                    candidate.save(buf, "JPEG", quality=quality, optimize=True)
                    best = buf.getvalue()
                    if max_bytes is None or len(best) <= max_bytes:
                        return best, "image/jpeg"
            return best, "image/jpeg"
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise _bad_image(name) from exc


@dataclass
class ImagePayload:
    name: str
    data: bytes
    mime: str
    resized: bool = False
    level: int = -1  # индекс ступени IMAGE_SHRINK_LADDER, до которой уже уменьшено


@dataclass
class MediaPlan:
    images: list[ImagePayload] = field(default_factory=list)
    native_pdfs: list[ParsedFile] = field(default_factory=list)
    text_pdfs: list[ParsedFile] = field(default_factory=list)

    @property
    def resized_names(self) -> list[str]:
        return [i.name for i in self.images if i.resized]

    @property
    def native_bytes(self) -> int:
        return sum(len(i.data) for i in self.images) + sum(len(p.data) for p in self.native_pdfs)


def _pdf_fits_natively(provider: str, pdf: ParsedFile, pages: int, is_200k_context: bool) -> bool:
    if provider == PROVIDER_ANTHROPIC:
        max_pages = (
            limits.ANTHROPIC_PDF_MAX_PAGES_200K_CONTEXT
            if is_200k_context
            else limits.ANTHROPIC_PDF_MAX_PAGES
        )
        return len(pdf.data) <= limits.ANTHROPIC_REQUEST_RAW_BUDGET and pages <= max_pages
    if provider == PROVIDER_GEMINI:
        return len(pdf.data) <= limits.GEMINI_PDF_MAX_BYTES and pages <= limits.GEMINI_PDF_MAX_PAGES
    return False  # OpenAI Chat Completions: нативного PDF нет


def plan_media(
    provider: str,
    *,
    images: list[ParsedFile],
    pdfs: list[ParsedFile],
    pdf_pages: list[int],
    is_200k_context: bool = False,
) -> MediaPlan:
    """Решает, что и в каком виде уйдёт провайдеру.

    1. Изображения сверх per-image лимита провайдера (Anthropic: 7,5 МБ сырых и
       8000 px по стороне) уменьшаются.
    2. PDF вне лимита провайдера по размеру/страницам уходят текстовым путём.
    3. Если суммарный payload не помещается в бюджет запроса — сначала по
       ступеням уменьшаются самые крупные изображения, затем самые крупные
       нативные PDF переводятся в текстовый путь.
    """
    plan = MediaPlan()

    for image in images:
        payload = ImagePayload(image.filename, image.data, image.mime)
        if provider == PROVIDER_ANTHROPIC:
            width, height = image_size(image.data, image.filename)
            if (
                len(image.data) > limits.ANTHROPIC_IMAGE_MAX_RAW_BYTES
                or max(width, height) > limits.ANTHROPIC_IMAGE_MAX_EDGE
            ):
                payload.data, payload.mime = shrink_image(
                    image.data,
                    max_edge=min(
                        max(width, height),
                        limits.IMAGE_SHRINK_LADDER[0],
                        limits.ANTHROPIC_IMAGE_MAX_EDGE,
                    ),
                    max_bytes=limits.ANTHROPIC_IMAGE_MAX_RAW_BYTES,
                    name=image.filename,
                )
                payload.resized = True
        plan.images.append(payload)

    for pdf, pages in zip(pdfs, pdf_pages, strict=True):
        if _pdf_fits_natively(provider, pdf, pages, is_200k_context):
            plan.native_pdfs.append(pdf)
        else:
            plan.text_pdfs.append(pdf)

    budget = _request_budget(provider)
    originals = {id(p): img for p, img in zip(plan.images, images, strict=True)}
    for _ in range(100):  # жёсткий потолок итераций
        if plan.native_bytes <= budget:
            break
        shrinkable = [
            p
            for p in plan.images
            if p.level < len(limits.IMAGE_SHRINK_LADDER) - 1
            and len(p.data) > limits.IMAGE_SHRINK_MIN_BYTES
        ]
        if shrinkable:
            target = max(shrinkable, key=lambda p: len(p.data))
            target.level += 1
            original = originals[id(target)]
            target.data, target.mime = shrink_image(
                original.data,
                max_edge=limits.IMAGE_SHRINK_LADDER[target.level],
                name=target.name,
            )
            target.resized = True
        elif plan.native_pdfs:
            biggest = max(plan.native_pdfs, key=lambda p: len(p.data))
            plan.native_pdfs.remove(biggest)
            plan.text_pdfs.append(biggest)
        else:
            break
    return plan
