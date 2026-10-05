from __future__ import annotations

import asyncio
import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, get_current_user
from app.db.session import get_db
from app.limits import MAX_DOCUMENT_BYTES, MAX_EXTRACTED_CHARS_PER_FILE
from app.models.chat import ChatSession
from app.utils.attachments import store_user_upload, with_urls
from app.utils.r2 import StorageNotConfiguredError
from app.utils.uploads import DOCUMENT_ONLY_EXTENSIONS, classify_upload, read_upload

router = APIRouter(prefix="/v1/documents", tags=["documents"])

MAX_FILE_SIZE_BYTES = MAX_DOCUMENT_BYTES  # 50 МБ, читается кусками (см. utils/uploads.py)
MAX_EXTRACTED_CHARS = MAX_EXTRACTED_CHARS_PER_FILE  # 60 000
SUPPORTED_EXTENSIONS = DOCUMENT_ONLY_EXTENSIONS  # docx, pdf, xlsx


def _extract_docx(content: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(content))
    parts = [p.text for p in document.paragraphs if p.text.strip()]

    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))

    return "\n".join(parts)


def _extract_pdf(content: bytes) -> str:
    from app.utils.pdf import extract_pdf_pages

    return "\n".join(extract_pdf_pages(content))


def _extract_xlsx(content: bytes) -> str:
    from openpyxl import load_workbook

    workbook = load_workbook(filename=io.BytesIO(content), read_only=True, data_only=True)
    parts: list[str] = []

    for sheet in workbook.worksheets:
        parts.append(f"# Лист: {sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            cells = [str(value) for value in row if value is not None]
            if cells:
                parts.append(" | ".join(cells))

    return "\n".join(parts)


EXTRACTORS = {
    ".docx": _extract_docx,
    ".pdf": _extract_pdf,
    ".xlsx": _extract_xlsx,
}


@router.post("/extract")
async def extract_document_text(
    file: UploadFile = File(...),
    # Необязательный чат, к которому относится файл. Без него файл кладётся в
    # ai/{user_id}/_pending/uploads/ и привязывается к чату через attachment_keys.
    session_id: str | None = Form(None),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Имя файла отсутствует")

    supported = f"Поддерживаются только файлы: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"

    sid: int | None = None
    if session_id is not None and session_id.strip() not in ("", "null", "undefined"):
        try:
            sid = int(session_id)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail="session_id должен быть целым числом"
            ) from exc
        session = await db.get(ChatSession, sid)
        if session is None or session.user_id != user.user_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Сессия не найдена")

    # Чтение кусками по 1 МБ с остановкой при превышении 50 МБ; тип — по
    # расширению и magic bytes (Content-Type клиента не используется).
    content = await read_upload(file)
    parsed = classify_upload(
        file.filename,
        content,
        allowed_exts=SUPPORTED_EXTENSIONS,
        unsupported_message=supported,
    )
    suffix = "." + parsed.kind

    # Сначала извлекаем текст (в потоке: pdfplumber/openpyxl — CPU-тяжёлые):
    # нечитаемый файл не должен оставлять объект в R2.
    try:
        text = await asyncio.to_thread(EXTRACTORS[suffix], content)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Не удалось прочитать файл: {exc}",
        ) from exc

    try:
        record = await store_user_upload(user.user_id, sid, file.filename, content, parsed.mime)
    except StorageNotConfiguredError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="Хранилище файлов не настроено"
        ) from exc

    attachment = with_urls([record])[0]

    truncated = len(text) > MAX_EXTRACTED_CHARS
    if truncated:
        text = text[:MAX_EXTRACTED_CHARS]

    return {
        "filename": file.filename,
        "text": text,
        "truncated": truncated,
        "char_count": len(text),
        "file_key": record["key"],
        "file_url": attachment["url"],  # presigned, как и attachment.url
        "attachment": attachment,
    }
