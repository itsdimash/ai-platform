"""Генерация Word-документа (.docx) по вызову инструмента generate_document.

Зеркалит app/utils/pptx_builder.py: модель отдаёт структурированные данные
(заголовок + секции), мы собираем реальный .docx через python-docx и
возвращаем байты (загрузка в R2 — в app/tools).
"""

import io

from docx import Document

from app.utils.tool_schema import SUMMARY_PROP

DOCUMENT_TOOL = {
    "name": "generate_document",
    "description": (
        "Generates a Word document (.docx) with a title and one or more "
        "sections (each with an optional heading and body paragraphs), and "
        "delivers it as a downloadable attachment. Use this when the user asks for a "
        "document, report, memo, letter, contract draft, or any text-heavy "
        "deliverable they want to download or send elsewhere — NOT for "
        "short answers that fit fine as a normal chat reply."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary": SUMMARY_PROP,
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
        "required": ["title", "sections", "summary"],
    },
}


def build_document(title: str, sections: list) -> bytes:
    """Собирает .docx и возвращает байты (загрузка в R2 — в app/tools)."""
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
    return stream.getvalue()
