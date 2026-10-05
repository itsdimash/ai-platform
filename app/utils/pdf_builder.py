"""Генерация PDF по вызову инструмента generate_pdf (reportlab, чистый Python).

Кириллица: встроенные шрифты reportlab её не содержат, поэтому используется
DejaVu Sans из app/assets/fonts (лицензия — рядом, LICENSE_DEJAVU.txt).
"""

import io
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.utils.tool_schema import SUMMARY_PROP

_FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

PDF_TOOL = {
    "name": "generate_pdf",
    "description": (
        "Generates a PDF document with a title and one or more sections (each with "
        "an optional heading, paragraphs, bullet points and an optional table) and "
        "delivers it to the user as a downloadable attachment. Use this when the "
        "user asks for a PDF file, a printable report, a brochure or an official "
        "document in PDF — NOT for short answers that fit a normal chat reply."
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
                        "bullets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional bullet list shown after the paragraphs.",
                        },
                        "table": {
                            "type": "object",
                            "description": "Optional table shown after the bullets.",
                            "properties": {
                                "headers": {"type": "array", "items": {"type": "string"}},
                                "rows": {
                                    "type": "array",
                                    "items": {"type": "array", "items": {"type": "string"}},
                                },
                            },
                            "required": ["headers", "rows"],
                        },
                    },
                },
            },
        },
        "required": ["title", "sections", "summary"],
    },
}


@lru_cache
def _register_fonts() -> None:
    for name, filename in (
        ("DejaVu", "DejaVuSans.ttf"),
        ("DejaVu-Bold", "DejaVuSans-Bold.ttf"),
        ("DejaVu-Oblique", "DejaVuSans-Oblique.ttf"),
        ("DejaVu-BoldOblique", "DejaVuSans-BoldOblique.ttf"),
    ):
        pdfmetrics.registerFont(TTFont(name, str(_FONTS_DIR / filename)))
    pdfmetrics.registerFontFamily(
        "DejaVu",
        normal="DejaVu",
        bold="DejaVu-Bold",
        italic="DejaVu-Oblique",
        boldItalic="DejaVu-BoldOblique",
    )


def _styles() -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle(
            "title", fontName="DejaVu-Bold", fontSize=22, leading=28, spaceAfter=14
        ),
        "h1": ParagraphStyle(
            "h1", fontName="DejaVu-Bold", fontSize=15, leading=20, spaceBefore=12, spaceAfter=6
        ),
        "body": ParagraphStyle("body", fontName="DejaVu", fontSize=10.5, leading=15, spaceAfter=6),
        "cell": ParagraphStyle("cell", fontName="DejaVu", fontSize=9, leading=12),
        "cell_head": ParagraphStyle(
            "cell_head", fontName="DejaVu-Bold", fontSize=9, leading=12, textColor=colors.white
        ),
    }


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    # Модель присылает обычный текст: экранируем разметку reportlab.
    return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)


def _table(table: dict, styles: dict[str, ParagraphStyle], width: float) -> Table | None:
    headers = [str(h) for h in table.get("headers", [])]
    rows = [[str(c) for c in row] for row in table.get("rows", [])]
    if not headers and not rows:
        return None
    ncols = max([len(headers)] + [len(r) for r in rows])
    data = []
    if headers:
        data.append([_p(h, styles["cell_head"]) for h in headers + [""] * (ncols - len(headers))])
    for row in rows:
        data.append([_p(c, styles["cell"]) for c in row + [""] * (ncols - len(row))])
    flowable = Table(data, colWidths=[width / ncols] * ncols, repeatRows=1 if headers else 0)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B8BEC6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    if headers:
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2F3B52")))
    flowable.setStyle(TableStyle(style))
    return flowable


def build_pdf(title: str, sections: list) -> bytes:
    """Собирает PDF и возвращает байты (загрузка в R2 — в app/tools)."""
    _register_fonts()
    styles = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=str(title),
    )
    width = A4[0] - doc.leftMargin - doc.rightMargin

    story: list = [_p(title, styles["title"])]
    for section in sections:
        heading = (section.get("heading") or "").strip()
        if heading:
            story.append(_p(heading, styles["h1"]))
        for paragraph in section.get("paragraphs") or []:
            story.append(_p(paragraph, styles["body"]))
        bullets = [b for b in (section.get("bullets") or []) if str(b).strip()]
        if bullets:
            story.append(
                ListFlowable(
                    [ListItem(_p(b, styles["body"]), leftIndent=12) for b in bullets],
                    bulletType="bullet",
                    bulletFontName="DejaVu",
                    start="•",
                    leftIndent=14,
                )
            )
        if section.get("table"):
            table = _table(section["table"], styles, width)
            if table is not None:
                story.extend([Spacer(1, 4), table, Spacer(1, 8)])

    doc.build(story)
    return buffer.getvalue()
