"""Тонкая обёртка над boto3 для Cloudflare R2.

Вызовы boto3 синхронные — из async-кода использовать *_async-обёртки (to_thread).
presign_get считается локально (подпись), без сетевого запроса.

Публичные URL (R2_PUBLIC_DOMAIN) для префикса ai/ не используются вообще:
файлы отдаются только presigned-ссылками.
"""

import asyncio
import urllib.parse
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import get_settings
from app.utils.storage import content_disposition, is_inline_mime


class StorageNotConfiguredError(RuntimeError):
    """R2 не настроен в окружении (см. R2_* в .env.example)."""


@lru_cache
def get_s3_client():
    settings = get_settings()
    if not (settings.r2_account_id and settings.r2_access_key and settings.r2_secret_key):
        raise StorageNotConfiguredError(
            "R2 не настроен: задайте R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY"
        )
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key,
        aws_secret_access_key=settings.r2_secret_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _bucket() -> str:
    bucket = get_settings().r2_bucket_name
    if not bucket:
        raise StorageNotConfiguredError("R2 не настроен: задайте R2_BUCKET_NAME")
    return bucket


def put_bytes(key: str, data: bytes, content_type: str, *, name: str | None = None) -> None:
    """Кладёт объект под точным ключом. Оригинальное имя файла сохраняется в
    метаданных (URL-кодированное: S3-метаданные — только ASCII)."""
    extra: dict = {}
    if name:
        extra["Metadata"] = {"name": urllib.parse.quote(name, safe="")}
    get_s3_client().put_object(
        Bucket=_bucket(), Key=key, Body=data, ContentType=content_type, **extra
    )


async def put_bytes_async(
    key: str, data: bytes, content_type: str, *, name: str | None = None
) -> None:
    await asyncio.to_thread(put_bytes, key, data, content_type, name=name)


def head_info(key: str) -> dict | None:
    """{"size", "mime", "name"} или None, если объекта нет."""
    try:
        head = get_s3_client().head_object(Bucket=_bucket(), Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    raw_name = (head.get("Metadata") or {}).get("name")
    return {
        "size": int(head.get("ContentLength", 0)),
        "mime": head.get("ContentType") or "application/octet-stream",
        "name": urllib.parse.unquote(raw_name) if raw_name else None,
    }


async def head_info_async(key: str) -> dict | None:
    return await asyncio.to_thread(head_info, key)


def presign_get(
    key: str,
    expires: int | None = None,
    download_name: str | None = None,
    inline: bool | None = None,
    content_type: str | None = None,
) -> str:
    """Presigned GET. download_name задаёт Content-Disposition (RFC 5987:
    ASCII-fallback в filename + filename*=UTF-8''...). inline: True — показать
    в браузере (изображения, PDF), False — скачать; None — решить по
    content_type. content_type, если известен, фиксируется в ответе."""
    settings = get_settings()
    ttl = expires if expires is not None else settings.r2_presign_expires
    params: dict = {"Bucket": _bucket(), "Key": key}
    if download_name:
        if inline is None:
            inline = is_inline_mime(content_type or "")
        params["ResponseContentDisposition"] = content_disposition(download_name, inline=inline)
    if content_type:
        params["ResponseContentType"] = content_type
    return get_s3_client().generate_presigned_url("get_object", Params=params, ExpiresIn=ttl)
