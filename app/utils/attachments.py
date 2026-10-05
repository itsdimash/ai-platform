"""Вложения сообщений: presigned-ссылки при чтении, проверка ключей из запроса,
сохранение файлов пользователя."""

import asyncio
import logging
import re

from fastapi import HTTPException

from app.utils.r2 import StorageNotConfiguredError, head_info_async, presign_get, put_bytes_async
from app.utils.storage import (
    build_key,
    display_name,
    ensure_extension,
    is_inline_mime,
    make_record,
    owns_key,
)

logger = logging.getLogger(__name__)

_UUID_PREFIX = re.compile(r"^[0-9a-f]{32}_")


def with_urls(records: list[dict] | None) -> list[dict]:
    """Добавляет свежий presigned url к записям вложений (в БД url не хранится).
    Сбой подписи (например, R2 не настроен) не ломает историю: url=None."""
    result = []
    for rec in records or []:
        try:
            url = presign_get(
                rec["key"],
                download_name=rec.get("name"),
                inline=is_inline_mime(rec.get("mime", "")),
                content_type=rec.get("mime"),
            )
        except (StorageNotConfiguredError, KeyError):
            logger.warning("presign failed for attachment", exc_info=True)
            url = None
        result.append({**rec, "url": url})
    return result


_KIND_BY_MIME = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}


def kinds_of_records(records: list[dict]) -> list[str]:
    """Виды вложений для классификатора: image | pdf | docx | xlsx | file."""
    return [
        "image" if r.get("type") == "image" else _KIND_BY_MIME.get(r.get("mime", ""), "file")
        for r in records
    ]


def describe_kinds(kinds: list[str]) -> str:
    """["pdf", "image", "image"] -> "attached: 1 pdf, 2 images" ("" если вложений нет)."""
    if not kinds:
        return ""
    parts = []
    for kind in dict.fromkeys(kinds):
        n = kinds.count(kind)
        label = "images" if kind == "image" and n > 1 else kind
        parts.append(f"{n} {label}")
    return "attached: " + ", ".join(parts)


def _name_from_key(key: str) -> str:
    return _UUID_PREFIX.sub("", key.rsplit("/", 1)[-1])


async def resolve_attachment_keys(keys: list[str], user_id: int) -> list[dict]:
    """Проверяет ключи из запроса: строго собственные (ai/{user_id}/..., роль не
    учитывается) и существующие. Размер и mime берутся из самого объекта, а не от
    клиента. 403 — чужой/некорректный ключ, 404 — объекта нет.

    Только привязка: содержимое объекта не читается (используется HEAD), текст в
    промпт не попадает. Повторы одного ключа в запросе схлопываются, порядок —
    порядок первого появления."""
    unique = list(dict.fromkeys(keys))
    for key in unique:
        if not owns_key(key, user_id):
            raise HTTPException(status_code=403, detail="Недопустимый ключ вложения")
    try:
        infos = await asyncio.gather(*(head_info_async(k) for k in unique))
    except StorageNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="Хранилище файлов не настроено") from exc
    records = []
    for key, info in zip(unique, infos, strict=True):
        if info is None:
            raise HTTPException(status_code=404, detail="Вложение не найдено")
        records.append(
            make_record(
                name=info["name"] or _name_from_key(key),
                key=key,
                mime=info["mime"],
                size=info["size"],
            )
        )
    return records


async def store_user_upload(
    user_id: int, session_id: int | None, filename: str, data: bytes, mime: str
) -> dict:
    """Сохраняет файл пользователя в ai/{user_id}/{session_id}/uploads/...,
    возвращает запись вложения."""
    name = ensure_extension(display_name(filename, default="attachment"), mime)
    key = build_key(user_id, session_id, name, uploads=True)
    await put_bytes_async(key, data, mime, name=name)
    return make_record(name=name, key=key, mime=mime, size=len(data))
