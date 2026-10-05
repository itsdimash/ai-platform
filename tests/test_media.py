import io
import random

import pytest
from fastapi import HTTPException
from PIL import Image

from app import limits
from app.utils.media import (
    PROVIDER_ANTHROPIC,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    image_size,
    plan_media,
    shrink_image,
)
from app.utils.uploads import ParsedFile


def noisy_png(w, h, seed=1):
    rng = random.Random(seed)
    im = Image.new("RGB", (w, h))
    im.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(w * h)])
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def img(name, data, mime="image/png"):
    return ParsedFile(name, data, mime, "image")


def pdf(name, size):
    return ParsedFile(name, b"%PDF-" + b"\x00" * size, "application/pdf", "pdf")


def test_shrink_image_respects_edge_and_bytes_and_flattens_alpha():
    rgba = Image.new("RGBA", (400, 300), (255, 0, 0, 0))
    buf = io.BytesIO()
    rgba.save(buf, "PNG")
    out, mime = shrink_image(buf.getvalue(), max_edge=100, max_bytes=50_000)
    assert mime == "image/jpeg"
    w, h = image_size(out)
    assert max(w, h) <= 100
    assert len(out) <= 50_000


def test_shrink_image_bad_data_is_400():
    with pytest.raises(HTTPException) as e:
        shrink_image(b"not an image", max_edge=100)
    assert e.value.status_code == 400


def test_anthropic_oversized_image_is_downscaled_original_untouched(monkeypatch):
    monkeypatch.setattr(limits, "ANTHROPIC_IMAGE_MAX_RAW_BYTES", 60_000)
    original = noisy_png(300, 300)
    assert len(original) > 60_000
    image = img("big.png", original)

    plan = plan_media(PROVIDER_ANTHROPIC, images=[image], pdfs=[], pdf_pages=[])

    payload = plan.images[0]
    assert payload.resized and payload.mime == "image/jpeg"
    assert len(payload.data) <= 60_000
    assert image.data == original  # оригинал (он уйдёт в R2) не тронут
    assert plan.resized_names == ["big.png"]


def test_anthropic_edge_limit_triggers_downscale(monkeypatch):
    monkeypatch.setattr(limits, "ANTHROPIC_IMAGE_MAX_EDGE", 200)
    plan = plan_media(
        PROVIDER_ANTHROPIC, images=[img("wide.png", noisy_png(300, 100))], pdfs=[], pdf_pages=[]
    )
    assert plan.images[0].resized
    assert max(image_size(plan.images[0].data)) <= 200


def test_small_image_passes_through_unchanged():
    data = noisy_png(40, 40)
    plan = plan_media(PROVIDER_ANTHROPIC, images=[img("s.png", data)], pdfs=[], pdf_pages=[])
    assert plan.images[0].data == data and not plan.images[0].resized


def test_openai_and_gemini_do_not_resize_without_budget_pressure():
    data = noisy_png(200, 200)
    for provider in (PROVIDER_OPENAI, PROVIDER_GEMINI):
        plan = plan_media(provider, images=[img("a.png", data)], pdfs=[], pdf_pages=[])
        assert plan.images[0].data == data and not plan.images[0].resized


@pytest.mark.parametrize(
    ("provider", "is_200k", "pages", "native"),
    [
        (PROVIDER_ANTHROPIC, False, 600, True),
        (PROVIDER_ANTHROPIC, False, 601, False),
        (PROVIDER_ANTHROPIC, True, 100, True),  # Haiku 4.5: контекст 200K
        (PROVIDER_ANTHROPIC, True, 101, False),
        (PROVIDER_GEMINI, False, 1000, True),
        (PROVIDER_GEMINI, False, 1001, False),
        (PROVIDER_OPENAI, False, 1, False),  # нативного PDF нет
    ],
)
def test_pdf_native_vs_text_by_provider_and_pages(provider, is_200k, pages, native):
    plan = plan_media(
        provider, images=[], pdfs=[pdf("a.pdf", 1000)], pdf_pages=[pages], is_200k_context=is_200k
    )
    assert bool(plan.native_pdfs) is native and bool(plan.text_pdfs) is not native


def test_pdf_over_provider_byte_limit_goes_to_text(monkeypatch):
    monkeypatch.setattr(limits, "ANTHROPIC_REQUEST_RAW_BUDGET", 10_000)
    plan = plan_media(PROVIDER_ANTHROPIC, images=[], pdfs=[pdf("big.pdf", 20_000)], pdf_pages=[5])
    assert plan.text_pdfs and not plan.native_pdfs

    monkeypatch.setattr(limits, "GEMINI_PDF_MAX_BYTES", 10_000)
    plan = plan_media(PROVIDER_GEMINI, images=[], pdfs=[pdf("big.pdf", 20_000)], pdf_pages=[5])
    assert plan.text_pdfs and not plan.native_pdfs


def test_total_budget_shrinks_images_before_demoting_pdfs(monkeypatch):
    monkeypatch.setattr(limits, "GEMINI_REQUEST_RAW_BUDGET", 150_000)
    monkeypatch.setattr(limits, "IMAGE_SHRINK_MIN_BYTES", 1000)
    monkeypatch.setattr(limits, "IMAGE_SHRINK_LADDER", (200, 100, 50))
    big = noisy_png(400, 400)
    plan = plan_media(
        PROVIDER_GEMINI,
        images=[img("a.png", big), img("b.png", big)],
        pdfs=[pdf("a.pdf", 20_000)],
        pdf_pages=[3],
    )
    assert plan.native_bytes <= 150_000
    assert all(p.resized for p in plan.images)
    assert plan.native_pdfs  # PDF остался нативным — хватило сжатия картинок


def test_total_budget_demotes_largest_pdf_when_images_cannot_help(monkeypatch):
    monkeypatch.setattr(limits, "OPENAI_REQUEST_RAW_BUDGET", 1)  # заведомо не влезает
    plan = plan_media(PROVIDER_OPENAI, images=[], pdfs=[pdf("a.pdf", 100)], pdf_pages=[1])
    assert plan.native_bytes == 0 or not plan.native_pdfs

    monkeypatch.setattr(limits, "GEMINI_REQUEST_RAW_BUDGET", 50_000)
    plan = plan_media(
        PROVIDER_GEMINI,
        images=[],
        pdfs=[pdf("small.pdf", 10_000), pdf("huge.pdf", 45_000)],
        pdf_pages=[1, 1],
    )
    assert [p.filename for p in plan.native_pdfs] == ["small.pdf"]
    assert [p.filename for p in plan.text_pdfs] == ["huge.pdf"]
