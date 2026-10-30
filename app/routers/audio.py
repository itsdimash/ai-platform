"""Голосовой ввод: POST /v1/audio/transcribe — аудио -> текст.

Фронт записывает речь через MediaRecorder и присылает короткий файл (webm/ogg/mp4/...).
Возвращается только распознанный текст: в историю чата он не пишется, пользователь
сначала правит его в поле ввода и отправляет обычным сообщением.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from openai import AsyncOpenAI, OpenAIError

from ..auth import CurrentUser, get_current_user
from ..config import get_settings
from ..limits import CHUNK_SIZE, MAX_AUDIO_BYTES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/audio", tags=["audio"])

# Форматы, которые принимает OpenAI transcription API (и которые отдают браузеры).
AUDIO_EXTENSIONS = frozenset(
    {".webm", ".ogg", ".oga", ".mp4", ".m4a", ".mp3", ".mpga", ".mpeg", ".wav", ".flac"}
)
TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"


async def _read_limited(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(CHUNK_SIZE):
        total += len(chunk)
        if total > MAX_AUDIO_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Запись слишком большая (максимум {MAX_AUDIO_BYTES // (1024 * 1024)} МБ)",
            )
        chunks.append(chunk)
    if total == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Запись пустая")
    return b"".join(chunks)


@router.post("/transcribe")
async def transcribe_audio(
    file: UploadFile = File(...),
    # Подсказка языка (ISO-639-1, например "ru"); без неё модель определяет язык сама.
    language: str | None = Form(None),
    _user: CurrentUser = Depends(get_current_user),
) -> dict:
    name = os.path.basename(file.filename or "voice.webm")
    ext = os.path.splitext(name)[1].lower()
    if ext not in AUDIO_EXTENSIONS:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Формат записи не поддерживается. Разрешены: {', '.join(sorted(AUDIO_EXTENSIONS))}",
        )

    settings = get_settings()
    if not settings.openai_api_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="Распознавание речи не настроено"
        )

    data = await _read_limited(file)

    lang = (language or "").strip().lower()
    kwargs = {"language": lang} if len(lang) == 2 and lang.isalpha() else {}

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    try:
        result = await client.audio.transcriptions.create(
            model=TRANSCRIBE_MODEL,
            file=(name, data, file.content_type or "application/octet-stream"),
            **kwargs,
        )
    except OpenAIError as exc:
        logger.exception("audio transcription failed")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail="Не удалось распознать запись"
        ) from exc
    finally:
        await client.close()

    return {"text": (result.text or "").strip()}
