"""Хранилище файлов для шаблонов (R2). Протокол — чтобы тесты подставляли память."""

from typing import Protocol

from app.utils.r2 import get_bytes_async, head_info_async, put_bytes_async


class TemplateStorage(Protocol):
    async def head(self, key: str) -> dict | None: ...  # {"size","mime","name"} или None

    async def read(self, key: str) -> bytes: ...

    async def write(self, key: str, data: bytes, mime: str, name: str) -> None: ...


class R2Storage:
    async def head(self, key: str) -> dict | None:
        return await head_info_async(key)

    async def read(self, key: str) -> bytes:
        return await get_bytes_async(key)

    async def write(self, key: str, data: bytes, mime: str, name: str) -> None:
        await put_bytes_async(key, data, mime, name=name)
