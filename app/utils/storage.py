"""Чистая логика хранилища: ключи объектов, права доступа, Content-Disposition.

Здесь нет boto3 и сети — всё тестируется без R2.

Модель доступа для префикса ai/ — «ссылка-секрет»: bucket общий с ERP и публичный,
поэтому защита объекта — это 128-битный uuid4 в ключе плюс то, что ai-platform
отдаёт ссылки только presigned и только владельцу (или ролям из ADMIN_ROLES).
Права проверяются здесь, до выдачи ссылки.
"""

import re
import unicodedata
import urllib.parse
import uuid

AI_PREFIX = "ai"
PENDING_SEGMENT = "_pending"  # файл загружен до того, как у чата появился session_id
UPLOADS_SEGMENT = "uploads"
ADMIN_ROLES = frozenset({"admin", "commercial_director"})

MAX_KEY_LENGTH = 512
_MAX_SAFE_NAME = 80
_MAX_DISPLAY_NAME = 120

_UNSAFE_KEY_CHARS = re.compile(r"[^\w.\-]+", re.UNICODE)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_FORBIDDEN_DISPLAY_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def _split_ext(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    if dot and stem and 0 < len(ext) <= 8 and ext.isalnum():
        return stem, "." + ext.lower()
    return name, ""


def safe_name(name: str) -> str:
    """Имя для ключа объекта: без путей и спецсимволов, кириллица сохраняется."""
    name = unicodedata.normalize("NFC", name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    stem, ext = _split_ext(name)
    stem = _UNSAFE_KEY_CHARS.sub("_", stem).strip("._-") or "file"
    return stem[: max(1, _MAX_SAFE_NAME - len(ext))] + ext


def display_name(name: str, default: str = "file") -> str:
    """Читаемое имя файла для пользователя (хранится в attachments.name)."""
    name = unicodedata.normalize("NFC", name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _FORBIDDEN_DISPLAY_CHARS.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return default
    stem, ext = _split_ext(name)
    return stem[: max(1, _MAX_DISPLAY_NAME - len(ext))].rstrip() + ext


_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}


def ensure_extension(name: str, mime: str) -> str:
    """Добавляет расширение по mime, если у имени его нет (вставка из буфера обмена:
    «clipboard» -> «clipboard.png»), чтобы скачанный файл открывался правильно."""
    if _split_ext(name)[1]:
        return name
    ext = _EXT_BY_MIME.get((mime or "").lower())
    return f"{name}{ext}" if ext else name


def build_key(user_id: int, session_id: int | None, filename: str, *, uploads: bool = False) -> str:
    """ai/{user_id}/{session_id}/[uploads/]{uuid4hex}_{safe_name}.

    Без session_id (файл загружен до создания чата) сегмент сессии — "_pending".
    """
    session_part = str(session_id) if session_id is not None else PENDING_SEGMENT
    parts = [AI_PREFIX, str(int(user_id)), session_part]
    if uploads:
        parts.append(UPLOADS_SEGMENT)
    parts.append(f"{uuid.uuid4().hex}_{safe_name(filename)}")
    return "/".join(parts)


def is_valid_key(key: str) -> bool:
    """Синтаксическая проверка ключа из внешнего ввода (путь URL / JSON)."""
    if not isinstance(key, str) or not key or len(key) > MAX_KEY_LENGTH:
        return False
    if _CONTROL_CHARS.search(key) or "\\" in key or ".." in key:
        return False
    if key.startswith("/") or key.endswith("/") or "//" in key:
        return False
    segments = key.split("/")
    return len(segments) >= 4 and segments[0] == AI_PREFIX


def owns_key(key: str, user_id: int) -> bool:
    """Ключ лежит строго под ai/{user_id}/ (роль не учитывается)."""
    return is_valid_key(key) and key.split("/")[1] == str(int(user_id))


def can_access_key(key: str, user_id: int, role: str) -> bool:
    """Владелец — всегда; роли из ADMIN_ROLES — любые ключи под ai/."""
    if not is_valid_key(key):
        return False
    return owns_key(key, user_id) or role in ADMIN_ROLES


def attachment_type_for_mime(mime: str) -> str:
    return "image" if mime.lower().startswith("image/") else "file"


def is_inline_mime(mime: str) -> bool:
    """Что браузер может безопасно показать на месте: изображения (кроме SVG) и PDF."""
    mime = (mime or "").lower()
    if mime == "image/svg+xml":
        return False
    return mime.startswith("image/") or mime == "application/pdf"


def content_disposition(download_name: str, *, inline: bool) -> str:
    """Content-Disposition по RFC 6266/5987: ASCII-fallback в filename и
    UTF-8 вариант в filename*."""
    name = display_name(download_name, default="download")
    stem, ext = _split_ext(name)
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    ascii_stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", ascii_stem).strip(" ._-") or "file"
    fallback = (ascii_stem + ext).replace("\\", "_").replace('"', "_")
    disposition = "inline" if inline else "attachment"
    return (
        f'{disposition}; filename="{fallback}"; '
        f"filename*=UTF-8''{urllib.parse.quote(name, safe='')}"
    )


def make_record(*, name: str, key: str, mime: str, size: int) -> dict:
    """Запись вложения в том виде, в котором она хранится в БД (без url)."""
    return {
        "type": attachment_type_for_mime(mime),
        "name": display_name(name),
        "key": key,
        "mime": mime,
        "size": int(size),
    }
