import os
import uuid
import boto3
from botocore.config import Config


def get_s3_client():
    account_id = os.getenv("R2_ACCOUNT_ID", "")
    access_key = os.getenv("R2_ACCESS_KEY", "")
    secret_key = os.getenv("R2_SECRET_KEY", "")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def upload_file_to_r2(file_bytes: bytes, original_filename: str, content_type: str, folder: str = "files") -> str:
    client = get_s3_client()
    bucket_name = os.getenv("R2_BUCKET_NAME", "ai-platform")
    public_domain = os.getenv("R2_PUBLIC_DOMAIN", "https://pub-cc9e792b4c9d455688e606c55f752b07.r2.dev").rstrip("/")

    safe_filename = original_filename.replace(" ", "_")
    unique_filename = f"{folder}/{uuid.uuid4().hex[:8]}_{safe_filename}"

    client.put_object(
        Bucket=bucket_name,
        Key=unique_filename,
        Body=file_bytes,
        ContentType=content_type,
    )

    return f"{public_domain}/{unique_filename}"