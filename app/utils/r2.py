import uuid
from functools import lru_cache

import boto3
from botocore.config import Config

from app.config import get_settings


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


def upload_file_to_r2(
    file_bytes: bytes, original_filename: str, content_type: str, folder: str = "files"
) -> str:
    """Синхронная загрузка (boto3 блокирующий) — из async-кода вызывать только
    через asyncio.to_thread / run_in_threadpool. Возвращает публичный URL."""
    settings = get_settings()
    if not settings.r2_bucket_name:
        raise StorageNotConfiguredError("R2 не настроен: задайте R2_BUCKET_NAME")
    if not settings.r2_public_domain:
        raise StorageNotConfiguredError("R2 не настроен: задайте R2_PUBLIC_DOMAIN")

    client = get_s3_client()
    public_domain = settings.r2_public_domain.rstrip("/")

    safe_filename = original_filename.replace(" ", "_")
    unique_filename = f"{folder}/{uuid.uuid4().hex[:8]}_{safe_filename}"

    client.put_object(
        Bucket=settings.r2_bucket_name,
        Key=unique_filename,
        Body=file_bytes,
        ContentType=content_type,
    )

    return f"{public_domain}/{unique_filename}"
