import io
import zipfile

import pytest
from fastapi import HTTPException

from app.limits import CHUNK_SIZE
from app.utils import uploads
from app.utils.docx_builder import build_document
from app.utils.pdf_builder import build_pdf
from app.utils.uploads import (
    DOCUMENT_ONLY_EXTENSIONS,
    classify_upload,
    extension_of,
    read_upload,
    sniff_family,
)
from app.utils.xlsx_builder import build_spreadsheet

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
DOCX = build_document("t", [{"paragraphs": ["x"]}])
XLSX = build_spreadsheet([{"name": "s", "headers": ["a"], "rows": [["1"]]}])
PDF = build_pdf("t", [{"paragraphs": ["x"]}])


def test_extension_of():
    assert extension_of("a.PNG") == ".png"
    assert extension_of("dir/sub.name.docx") == ".docx"
    assert extension_of("noext") == ""
    assert extension_of(".hidden") == ""
    assert extension_of(None) == ""


def test_sniff_family():
    assert sniff_family(PNG) == "png" and sniff_family(JPEG) == "jpeg"
    assert sniff_family(WEBP) == "webp" and sniff_family(PDF) == "pdf"
    assert sniff_family(DOCX) == "zip" and sniff_family(GIF) is None


@pytest.mark.parametrize(
    ("name", "data", "mime", "kind"),
    [
        ("a.png", PNG, "image/png", "image"),
        ("a.JPG", JPEG, "image/jpeg", "image"),
        ("a.jpeg", JPEG, "image/jpeg", "image"),
        ("a.webp", WEBP, "image/webp", "image"),
        ("a.pdf", PDF, "application/pdf", "pdf"),
        ("a.docx", DOCX, uploads.MIME_DOCX, "docx"),
        ("a.xlsx", XLSX, uploads.MIME_XLSX, "xlsx"),
    ],
)
def test_accepts_allowed_extension_with_matching_content(name, data, mime, kind):
    parsed = classify_upload(name, data)
    assert (parsed.mime, parsed.kind) == (mime, kind)


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("a.gif", GIF),
        ("a.heic", HEIC),
        ("a.svg", b"<svg></svg>"),
        ("a.exe", b"MZ\x90"),
        ("a.txt", b"hi"),
    ],
)
def test_rejects_unsupported_extensions(name, data):
    with pytest.raises(HTTPException) as e:
        classify_upload(name, data)
    assert e.value.status_code == 400


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("photo.png", PDF),  # PDF под видом картинки
        ("doc.pdf", PNG),
        ("book.docx", XLSX),  # docx vs xlsx различаются по составу архива
        ("book.xlsx", DOCX),
        ("fake.docx", b"PK\x03\x04not really a zip"),
        ("pic.jpg", PNG),
    ],
)
def test_rejects_content_that_contradicts_extension(name, data):
    with pytest.raises(HTTPException) as e:
        classify_upload(name, data)
    assert e.value.status_code == 400 and "не соответствует" in e.value.detail


def test_svg_content_is_rejected_even_with_image_extension():
    with pytest.raises(HTTPException):
        classify_upload("a.png", b'<?xml version="1.0"?><svg xmlns="..."></svg>')


@pytest.mark.parametrize(
    ("data", "mime"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp"), (PDF, "application/pdf")],
)
def test_no_extension_accepted_only_for_unambiguous_magic(data, mime):
    assert classify_upload("clipboard", data).mime == mime


def test_no_extension_zip_and_garbage_rejected():
    for data in (DOCX, GIF, b"hello"):
        with pytest.raises(HTTPException):
            classify_upload("blob", data)


def test_document_only_mode_rejects_images_with_custom_message():
    with pytest.raises(HTTPException) as e:
        classify_upload(
            "a.png",
            PNG,
            allowed_exts=DOCUMENT_ONLY_EXTENSIONS,
            unsupported_message="только документы",
        )
    assert e.value.detail == "только документы"
    assert classify_upload("a.docx", DOCX, allowed_exts=DOCUMENT_ONLY_EXTENSIONS).kind == "docx"


def test_zip_without_office_markers_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "x")
    with pytest.raises(HTTPException):
        classify_upload("a.docx", buf.getvalue())


class FakeUpload:
    """Минимальный UploadFile: считает, сколько реально прочитано."""

    def __init__(self, filename, data):
        self.filename = filename
        self._buf = io.BytesIO(data)
        self.bytes_read = 0

    async def read(self, size=-1):
        chunk = self._buf.read(size)
        self.bytes_read += len(chunk)
        return chunk


async def test_read_upload_small_file_roundtrip():
    data = PNG + b"\x01" * 3000
    assert await read_upload(FakeUpload("a.png", data)) == data
    assert await read_upload(FakeUpload("a.png", b"")) == b""


async def test_read_upload_exact_chunk_boundaries():
    for size in (CHUNK_SIZE - 1, CHUNK_SIZE, CHUNK_SIZE + 1, 2 * CHUNK_SIZE):
        data = PNG + b"\x02" * (size - len(PNG))
        assert await read_upload(FakeUpload("a.png", data)) == data


async def test_read_upload_stops_early_when_over_image_limit(monkeypatch):
    monkeypatch.setattr(uploads, "MAX_IMAGE_BYTES", 3 * CHUNK_SIZE)
    upload = FakeUpload("big.png", PNG + b"\x00" * (50 * CHUNK_SIZE))
    with pytest.raises(HTTPException) as e:
        await read_upload(upload)
    assert e.value.status_code == 400
    # остановились сразу после превышения, а не дочитали все 50 МБ
    assert upload.bytes_read <= 5 * CHUNK_SIZE


async def test_read_upload_document_limit_is_separate_from_image_limit(monkeypatch):
    monkeypatch.setattr(uploads, "MAX_IMAGE_BYTES", CHUNK_SIZE)
    monkeypatch.setattr(uploads, "MAX_DOCUMENT_BYTES", 4 * CHUNK_SIZE)
    doc = b"%PDF-" + b"\x00" * (2 * CHUNK_SIZE)
    assert len(await read_upload(FakeUpload("a.pdf", doc))) == len(
        doc
    )  # > лимита картинок, но в лимите документов
    with pytest.raises(HTTPException):
        await read_upload(FakeUpload("a.pdf", b"%PDF-" + b"\x00" * (6 * CHUNK_SIZE)))


async def test_read_upload_total_limit():
    upload = FakeUpload("a.png", PNG + b"\x00" * (3 * CHUNK_SIZE))
    with pytest.raises(HTTPException) as e:
        await read_upload(upload, total_so_far=0, max_total=2 * CHUNK_SIZE)
    assert "Суммарный" in e.value.detail
    assert upload.bytes_read <= 4 * CHUNK_SIZE
