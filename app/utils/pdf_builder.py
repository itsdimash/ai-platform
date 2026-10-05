"""Генерация PDF по вызову инструмента generate_pdf (reportlab, чистый Python).

Кириллица: встроенные шрифты reportlab её не содержат, поэтому используется DejaVu Sans из
app/assets/fonts (лицензия — рядом, LICENSE_DEJAVU.txt). Единая типографика: титульный блок,
заголовки, абзацы, маркированные и нумерованные списки, таблицы с шапкой и зеброй, номера
страниц «Стр. X из Y» (двухпроходный canvas) и колонтитул с названием документа.
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
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.utils.deck.themes import get_palette
from app.utils.tool_schema import SECTION_SCHEMA, SUMMARY_PROP

_FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

PDF_TOOL = {
    "name": "generate_pdf",
    "description": (
        "Create a PDF document: title block, sections with headings, paragraphs, bullet and numbered "
        "lists, tables. For printable reports, brochures, official documents. Complete finished text "
        "in the user's language, not an outline."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "subtitle": {"type": "string", "description": "Optional: date, author, addressee"},
            "summary": SUMMARY_PROP,
            "sections": {"type": "array", "items": SECTION_SCHEMA},
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


def _styles(p) -> dict[str, ParagraphStyle]:
    primary, text, muted = (
        colors.HexColor("#" + p.primary),
        colors.HexColor("#" + p.text),
        colors.HexColor("#" + p.muted),
    )
    return {
        "title": ParagraphStyle(
            "title",
            fontName="DejaVu-Bold",
            fontSize=22,
            leading=28,
            textColor=primary,
            spaceAfter=6,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", fontName="DejaVu", fontSize=11, leading=15, textColor=muted, spaceAfter=8
        ),
        "h1": ParagraphStyle(
            "h1",
            fontName="DejaVu-Bold",
            fontSize=15,
            leading=20,
            textColor=primary,
            spaceBefore=14,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "body", fontName="DejaVu", fontSize=10.5, leading=15.5, textColor=text, spaceAfter=7
        ),
        "cell": ParagraphStyle("cell", fontName="DejaVu", fontSize=9, leading=12, textColor=text),
        "cell_right": ParagraphStyle(
            "cell_right", fontName="DejaVu", fontSize=9, leading=12, textColor=text, alignment=2
        ),
        "cell_head": ParagraphStyle(
            "cell_head", fontName="DejaVu-Bold", fontSize=9, leading=12, textColor=colors.white
        ),
    }


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    # Модель присылает обычный текст: экранируем разметку reportlab.
    return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)


def _is_number(text: str) -> bool:
    cleaned = str(text).replace(" ", "").replace(" ", "").replace(",", ".").rstrip("%₸$€₽")
    cleaned = cleaned.lstrip("+-−–")
    try:
        float(cleaned)
    except ValueError:
        return False
    return True


def _table(table: dict, styles: dict[str, ParagraphStyle], width: float, p) -> Table | None:
    headers = [str(h) for h in table.get("headers") or []]
    rows = [[str(c) for c in row] for row in table.get("rows") or [] if isinstance(row, list)]
    if not headers and not rows:
        return None
    ncols = max([len(headers)] + [len(r) for r in rows])
    weights = [
        max(6, min(40, max((len(r[c]) for r in [headers, *rows] if c < len(r)), default=6)))
        for c in range(ncols)
    ]
    data = []
    if headers:
        data.append([_p(h, styles["cell_head"]) for h in headers + [""] * (ncols - len(headers))])
    for row in rows:
        padded = row + [""] * (ncols - len(row))
        data.append([_p(c, styles["cell_right" if _is_number(c) else "cell"]) for c in padded])
    flowable = Table(
        data, colWidths=[width * w / sum(weights) for w in weights], repeatRows=1 if headers else 0
    )
    style = [
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9D1D9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    start = 1 if headers else 0
    for i in range(start, len(data)):
        if (i - start) % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#" + p.surface)))
    if headers:
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#" + p.primary)))
    flowable.setStyle(TableStyle(style))
    return flowable


def _list(items: list[str], styles, kind: str) -> ListFlowable:
    return ListFlowable(
        [ListItem(_p(t, styles["body"]), leftIndent=16) for t in items],
        bulletType="1" if kind == "numbered" else "bullet",
        bulletFormat="%s." if kind == "numbered" else None,
        bulletFontName="DejaVu",
        start="•" if kind == "bullet" else None,
        leftIndent=18,
    )


def _numbered_canvas(title: str, muted_hex: str):
    """Canvas, который рисует «Стр. X из Y» и колонтитул после того, как известно число страниц."""

    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages: list[dict] = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._footer(total)
                super().showPage()
            super().save()

        def _footer(self, total: int) -> None:
            self.setFont("DejaVu", 8)
            self.setFillColor(colors.HexColor("#" + muted_hex))
            self.drawRightString(A4[0] - 20 * mm, 11 * mm, f"Стр. {self._pageNumber} из {total}")
            shown = title if len(title) < 70 else title[:67] + "…"
            self.drawString(20 * mm, 11 * mm, shown)

    return NumberedCanvas


def build_pdf(title: str, sections: list, subtitle: str = "") -> bytes:
    """Собирает PDF и возвращает байты (загрузка в R2 — в app/tools)."""
    _register_fonts()
    p = get_palette("graphite")
    styles = _styles(p)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=22 * mm,
        title=str(title),
        author="Kerneu AI",
    )
    width = A4[0] - doc.leftMargin - doc.rightMargin

    story: list = [_p(title, styles["title"])]
    if subtitle:
        story.append(_p(subtitle, styles["subtitle"]))
    story.append(
        HRFlowable(
            width="100%", thickness=1.6, color=colors.HexColor("#" + p.accent1), spaceAfter=8
        )
    )

    for section in sections:
        block: list = []
        heading = (section.get("heading") or "").strip()
        if heading:
            block.append(_p(heading, styles["h1"]))
        for paragraph in section.get("paragraphs") or []:
            block.append(_p(paragraph, styles["body"]))
        bullets = [b for b in (section.get("bullets") or []) if str(b).strip()]
        if bullets:
            block.append(_list(bullets, styles, "bullet"))
        numbered = [b for b in (section.get("numbered") or []) if str(b).strip()]
        if numbered:
            block.append(_list(numbered, styles, "numbered"))
        # заголовок не остаётся одиноким внизу страницы: держим вместе с первым блоком
        if block:
            story.append(KeepTogether(block[:2]))
            story.extend(block[2:])
        if isinstance(section.get("table"), dict):
            table = _table(section["table"], styles, width, p)
            if table is not None:
                story.extend([Spacer(1, 4), table, Spacer(1, 10)])

    doc.build(story, canvasmaker=_numbered_canvas(str(title), p.muted))
    return buffer.getvalue()
