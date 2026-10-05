from __future__ import annotations

import io

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.auth import CurrentUser, get_current_user
from app.utils.r2 import upload_file_to_r2

router = APIRouter(prefix="/v1/documents", tags=["documents"])

MAX_FILE_SIZE_BYTES = 15 * 1024 * 1024
MAX_EXTRACTED_CHARS = 60_000  # поднято с 20_000 — резало длинные договоры/отчёты
# задолго до реальных лимитов контекста моделей
SUPPORTED_EXTENSIONS = {".docx", ".pdf", ".xlsx"}


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
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Имя файла отсутствует")

    suffix = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Поддерживаются только файлы: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
        )

    content = await file.read()
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="Файл слишком большой (лимит 15 МБ)"
        )

    content_type = file.content_type or "application/octet-stream"
    file_url = upload_file_to_r2(
        file_bytes=content,
        original_filename=file.filename,
        content_type=content_type,
        folder="documents",
    )

    try:
        text = EXTRACTORS[suffix](content)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Не удалось прочитать файл: {exc}",
        ) from exc

    truncated = len(text) > MAX_EXTRACTED_CHARS
    if truncated:
        text = text[:MAX_EXTRACTED_CHARS]

    return {
        "filename": file.filename,
        "text": text,
        "truncated": truncated,
        "char_count": len(text),
        "file_url": file_url,
    }
