"""Генерация Word-документа (.docx) по вызову инструмента generate_document.

Зеркалит app/utils/pptx_builder.py: модель отдаёт структурированные данные
(заголовок + секции), мы собираем реальный .docx через python-docx и
заливаем в R2, возвращая публичную ссылку.
"""
import io

from docx import Document

from app.utils.r2 import upload_file_to_r2

DOCUMENT_TOOL = {
    "name": "generate_document",
    "description": (
        "Generates a Word document (.docx) with a title and one or more "
        "sections (each with an optional heading and body paragraphs), and "
        "returns a download link. Use this when the user asks for a "
        "document, report, memo, letter, contract draft, or any text-heavy "
        "deliverable they want to download or send elsewhere — NOT for "
        "short answers that fit fine as a normal chat reply."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Document title, rendered as the main heading.",
            },
            "sections": {
                "type": "array",
                "description": "Ordered list of sections making up the document body.",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {
                            "type": "string",
                            "description": "Section heading. Omit or leave empty for a section with no heading.",
                        },
                        "paragraphs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Paragraphs of body text for this section, in order.",
                        },
                    },
                    "required": ["paragraphs"],
                },
            },
        },
        "required": ["title", "sections"],
    },
}


def create_document_file(title: str, sections: list) -> str:
    doc = Document()
    doc.add_heading(title, level=0)

    for section in sections:
        heading = (section.get("heading") or "").strip()
        if heading:
            doc.add_heading(heading, level=1)
        for paragraph_text in section.get("paragraphs", []):
            doc.add_paragraph(paragraph_text)

    stream = io.BytesIO()
    doc.save(stream)
    file_bytes = stream.getvalue()

    safe_title = "".join(c if c.isalnum() else "_" for c in title)[:20]
    filename = f"{safe_title}.docx"

    return upload_file_to_r2(
        file_bytes=file_bytes,
        original_filename=filename,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        folder="documents",
    )
