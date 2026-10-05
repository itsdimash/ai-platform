"""Приём загруженных файлов: расширение + magic bytes, чтение кусками с лимитами.

Правила:
- расширение (если есть) должно быть из разрешённого набора: png, jpg/jpeg, webp,
  pdf, docx, xlsx (gif и heic не поддерживаются);
- тип определяется по magic bytes, а не по Content-Type клиента: пустой
  content-type и application/octet-stream принимаются, если расширение допустимо
  и содержимое ему соответствует;
- содержимое обязано соответствовать расширению (PDF под именем .png отклоняется);
  docx/xlsx — zip-контейнер, различаются по составу архива;
- без расширения принимаются только png/jpeg/webp/pdf (по magic bytes);
- размер проверяется ПРИ чтении кусками по 1 МБ — файл не читается целиком, если
  уже превысил лимит.
"""

import io
import zipfile
from dataclasses import dataclass

from fastapi import HTTPException, UploadFile, status

from app.limits import CHUNK_SIZE, MAX_DOCUMENT_BYTES, MAX_IMAGE_BYTES, MB

MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# расширение -> (mime, ожидаемое семейство magic bytes, вид файла)
EXTENSIONS: dict[str, tuple[str, str, str]] = {
    ".png": ("image/png", "png", "image"),
    ".jpg": ("image/jpeg", "jpeg", "image"),
    ".jpeg": ("image/jpeg", "jpeg", "image"),
    ".webp": ("image/webp", "webp", "image"),
    ".pdf": ("application/pdf", "pdf", "pdf"),
    ".docx": (MIME_DOCX, "zip", "docx"),
    ".xlsx": (MIME_XLSX, "zip", "xlsx"),
}
ALL_EXTENSIONS = frozenset(EXTENSIONS)
DOCUMENT_ONLY_EXTENSIONS = frozenset({".docx", ".pdf", ".xlsx"})

_FAMILY_DEFAULTS = {
    "png": ("image/png", "image"),
    "jpeg": ("image/jpeg", "image"),
    "webp": ("image/webp", "image"),
    "pdf": ("application/pdf", "pdf"),
}


@dataclass
class ParsedFile:
    filename: str
    data: bytes
    mime: str
    kind: str  # image | pdf | docx | xlsx


def _bad(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=detail)


def extension_of(filename: str | None) -> str:
    name = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name.strip(".") else ""


def sniff_family(head: bytes) -> str | None:
    """Семейство по magic bytes: png | jpeg | webp | pdf | zip | None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    return None


def looks_like_svg(head: bytes) -> bool:
    head = head[:512].lstrip().lower()
    return head.startswith(b"<?xml") or b"<svg" in head


def _limit_for(ext: str, family: str | None) -> int:
    if ext in EXTENSIONS:
        return MAX_IMAGE_BYTES if EXTENSIONS[ext][2] == "image" else MAX_DOCUMENT_BYTES
    if family in ("png", "jpeg", "webp"):
        return MAX_IMAGE_BYTES
    return MAX_DOCUMENT_BYTES


async def read_upload(
    upload: UploadFile, *, total_so_far: int = 0, max_total: int | None = None
) -> bytes:
    """Читает файл кусками по 1 МБ; останавливается, как только превышен лимит
    файла (20 МБ для изображений, 50 МБ для документов) или суммарный лимит запроса."""
    name = upload.filename or "attachment"
    ext = extension_of(name)

    first = await upload.read(CHUNK_SIZE)
    if not first:
        return b""
    limit = _limit_for(ext, sniff_family(first[:16]))

    buf = bytearray(first)
    chunk = first
    while True:
        size = len(buf)
        if size > limit:
            raise _bad(f"Файл {name!r} больше {limit // MB} МБ")
        if max_total is not None and total_so_far + size > max_total:
            raise _bad(f"Суммарный размер вложений превышает {max_total // MB} МБ")
        if len(chunk) < CHUNK_SIZE:
            break
        chunk = await upload.read(CHUNK_SIZE)
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _zip_kind(data: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return None
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    return None


def classify_upload(
    filename: str | None,
    data: bytes,
    *,
    allowed_exts: frozenset[str] = ALL_EXTENSIONS,
    unsupported_message: str | None = None,
) -> ParsedFile:
    """Проверяет расширение и содержимое, определяет mime/вид. Client Content-Type
    не используется вообще. Нарушение — HTTP 400."""
    name = filename or "attachment"
    ext = extension_of(name)
    head = data[:512]

    if looks_like_svg(head):
        raise _bad(f"SVG не поддерживается: {name!r}")

    if ext and ext not in allowed_exts:
        raise _bad(
            unsupported_message
            or f"Неподдерживаемое расширение {ext!r} у файла {name!r}. "
            f"Разрешены: {', '.join(sorted(allowed_exts))}"
        )

    family = sniff_family(head)

    if ext:
        mime, expected_family, kind = EXTENSIONS[ext]
        if family != expected_family:
            raise _bad(f"Содержимое файла {name!r} не соответствует расширению {ext}")
        if expected_family == "zip":
            actual = _zip_kind(data)
            if actual != kind:
                raise _bad(f"Содержимое файла {name!r} не соответствует расширению {ext}")
        return ParsedFile(name, data, mime, kind)

    # Без расширения — только то, что однозначно определяется по magic bytes.
    if family in _FAMILY_DEFAULTS:
        mime, kind = _FAMILY_DEFAULTS[family]
        if "." + {"jpeg": "jpg"}.get(family, family) in allowed_exts:
            return ParsedFile(name, data, mime, kind)
    raise _bad(
        unsupported_message
        or f"Не удалось определить тип файла {name!r}. Разрешены: {', '.join(sorted(allowed_exts))}"
    )
