"""Отрисовка колоды python-pptx: 16:9, собственные фигуры и текстовые поля (редактируются в
PowerPoint), нативные таблицы и диаграммы, заметки докладчика, номера слайдов (поле slidenum).

Основа — layout «Title Only» дефолтного шаблона: заголовок слайда остаётся настоящим
title-placeholder (виден в Outline/навигации и доступности), а геометрия мастера и layout'ов
масштабируется 4:3 -> 16:9, чтобы слайды, добавленные вручную, не выглядели сломанными. Всё
остальное рисуется поверх явными фигурами; цвета берутся из themes.Palette.
"""

import io
import re
from dataclasses import dataclass

from lxml import etree
from PIL import Image
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

from app.utils.deck import layout as L
from app.utils.deck.schema import Deck, Slide
from app.utils.deck.themes import (
    BODY_FLOOR,
    BODY_MAX,
    BULLET_MAX_FIVE,
    BULLET_MAX_SHORT,
    CAPTION,
    FONT,
    Palette,
    best_text_on,
    get_palette,
    mix,
)

Box = tuple[float, float, float, float]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


SLIDE_W_EMU, SLIDE_H_EMU = 12192000, 6858000  # 13,333 x 7,5 дюйма
_NUMERIC = re.compile(r"^[\s +\-–−]?[\d\s .,]+\s?[%₸$€₽]?$")


THIN = "\u202f"  # узкий неразрывный пробел между числом и единицей
_STAT_NUM = r"(?:\d[\d\s.,]*\d|\d)"
_STAT_SPLIT = re.compile(rf"^([+\-−–~≈]?\s*{_STAT_NUM}(?:\s*[–\-]\s*{_STAT_NUM})?)\s*(.*)$")


@dataclass
class RenderResult:
    data: bytes
    slide_count: int


def _rgb(hex6: str) -> RGBColor:
    return RGBColor.from_string(hex6.upper())


def prepare_picture(data: bytes, width_in: float, height_in: float) -> bytes:
    """Обрезка «cover» под пропорции рамки (без растяжения) + пережатие в JPEG ~1600 px."""
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        target = width_in / height_in
        w, h = im.size
        if w / h > target:  # шире рамки — режем по бокам
            new_w = int(h * target)
            im = im.crop(((w - new_w) // 2, 0, (w - new_w) // 2 + new_w, h))
        else:  # выше рамки — режем сверху/снизу
            new_h = int(w / target)
            im = im.crop((0, (h - new_h) // 2, w, (h - new_h) // 2 + new_h))
        im.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue()


class DeckRenderer:
    def __init__(self, deck: Deck, slides: list[Slide], images: dict) -> None:
        self.deck = deck
        self.slides = slides
        self.images = images  # {"hero": bytes, uid: bytes}
        self.p: Palette = get_palette(deck.theme)
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = Emu(SLIDE_W_EMU), Emu(SLIDE_H_EMU)
        self._rescale_master()
        self.layout = self.prs.slide_layouts[5]  # Title Only
        self.prs.core_properties.title = deck.title
        self.prs.core_properties.author = "Kerneu AI"

    # --- служебное -----------------------------------------------------------------------

    def _rescale_master(self) -> None:
        """Шаблон python-pptx — 4:3 (10 дюймов); растягиваем явные координаты по X на 16:9."""
        factor = SLIDE_W_EMU / 9144000
        containers = [self.prs.slide_master, *self.prs.slide_layouts]
        for container in containers:
            for shape in container.shapes:
                xfrm = shape._element.find(".//" + qn("a:xfrm"))
                if xfrm is None:
                    continue
                shape.left = int(shape.left * factor)
                shape.width = int(shape.width * factor)

    def _slide(self, bg: str) -> object:
        slide = self.prs.slides.add_slide(self.layout)
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = _rgb(bg)
        return slide

    @staticmethod
    def _emu(box: Box) -> tuple[Emu, Emu, Emu, Emu]:
        return tuple(Inches(v) for v in box)  # type: ignore[return-value]

    def _rect(
        self,
        slide,
        box: Box,
        fill: str,
        *,
        shape=MSO_SHAPE.RECTANGLE,
        line: str | None = None,
        name: str = "Shape",
    ):
        shp = slide.shapes.add_shape(shape, *self._emu(box))
        shp.fill.solid()
        shp.fill.fore_color.rgb = _rgb(fill)
        if line:
            shp.line.color.rgb = _rgb(line)
            shp.line.width = Pt(1.5)
        else:
            shp.line.fill.background()
        shp.shadow.inherit = False
        style = shp._element.find(qn("p:style"))  # LibreOffice рисует тень из effectRef темы
        if style is not None and style.find(qn("a:effectRef")) is not None:
            style.find(qn("a:effectRef")).set("idx", "0")
        shp.name = name
        return shp

    def _text(
        self,
        slide,
        box: Box,
        paras: list[str],
        size: float,
        color: str,
        *,
        bold: bool = False,
        italic: bool = False,
        align=PP_ALIGN.LEFT,
        anchor=MSO_ANCHOR.TOP,
        bullet: str | None = None,  # цвет маркера
        gap: float | None = None,  # интервал между абзацами, pt (по умолчанию 0,5 кегля)
        name: str = "Text",
    ):
        tb = slide.shapes.add_textbox(*self._emu(box))
        tb.name = name
        tf = tb.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = anchor
        for i, text in enumerate(paras):
            para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            para.alignment = align
            para.line_spacing = Pt(size * L.LINE_SPACING)
            para.space_after = (
                Pt(gap if gap is not None else size * 0.5) if i < len(paras) - 1 else Pt(0)
            )
            run = para.add_run()
            run.text = text
            self._style_run(run, size, color, bold, italic)
            if bullet:
                self._bullet(para, bullet)
        return tb

    @staticmethod
    def _style_run(run, size: float, color: str, bold: bool = False, italic: bool = False) -> None:
        run.font.name = FONT
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.italic = italic
        run.font.color.rgb = _rgb(color)

    @staticmethod
    def _bullet(para, color: str) -> None:
        ppr = para._p.get_or_add_pPr()
        ppr.set("marL", str(int(Inches(L.BULLET_INDENT))))
        ppr.set("indent", str(-int(Inches(L.BULLET_INDENT))))
        clr = etree.SubElement(ppr, qn("a:buClr"))
        etree.SubElement(clr, qn("a:srgbClr")).set("val", color.upper())
        etree.SubElement(ppr, qn("a:buFont")).set("typeface", "Arial")
        etree.SubElement(ppr, qn("a:buChar")).set("char", "•")

    def _title(
        self, slide, text: str, color: str, box: Box = L.TITLE_BOX, anchor=MSO_ANCHOR.BOTTOM
    ) -> None:
        ph = slide.shapes.title
        ph.left, ph.top, ph.width, ph.height = self._emu(box)
        pt, shown = L.title_fit(text, box[2], box[3])
        tf = ph.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = anchor
        para = tf.paragraphs[0]
        para.alignment = PP_ALIGN.LEFT
        para.line_spacing = Pt(pt * L.LINE_SPACING)
        for r in list(para.runs):
            r._r.getparent().remove(r._r)
        run = para.add_run()
        run.text = shown
        self._style_run(run, pt, color, bold=True)

    def _slide_number(self, slide, n: int, color: str) -> None:
        tb = slide.shapes.add_textbox(
            *self._emu((L.SLIDE_W - L.MX - 1.0, L.FOOTER_Y, 1.0, L.FOOTER_H))
        )
        tb.name = "Slide number"
        tf = tb.text_frame
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        para = tf.paragraphs[0]
        para.alignment = PP_ALIGN.RIGHT
        fld = etree.SubElement(para._p, qn("a:fld"))
        fld.set("id", "{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}")
        fld.set("type", "slidenum")
        rpr = etree.SubElement(fld, qn("a:rPr"))
        rpr.set("lang", "ru-RU")
        rpr.set("sz", str(CAPTION * 100))
        fill = etree.SubElement(rpr, qn("a:solidFill"))
        etree.SubElement(fill, qn("a:srgbClr")).set("val", color.upper())
        etree.SubElement(rpr, qn("a:latin")).set("typeface", FONT)
        etree.SubElement(fld, qn("a:t")).text = str(n)

    def _footer(self, slide, n: int, dark: bool) -> None:
        color = self.p.muted_on_dark if dark else self.p.muted
        text = self.deck.title
        while L.text_width(text, CAPTION) > 8.0 and len(text) > 8:
            text = text[:-2].rstrip() + "…"
        self._text(
            slide, (L.MX, L.FOOTER_Y, 8.0, L.FOOTER_H), [text], CAPTION, color, name="Footer"
        )
        self._slide_number(slide, n, color)

    def _notes(self, slide, text: str) -> None:
        if text:
            slide.notes_slide.notes_text_frame.text = text

    def _picture(self, slide, data: bytes, box: Box, alt: str, name: str = "Picture") -> None:
        pic = slide.shapes.add_picture(
            io.BytesIO(prepare_picture(data, box[2], box[3])), *self._emu(box)
        )
        pic.name = name
        c_nv = pic._element.nvPicPr.cNvPr
        c_nv.set("descr", alt[:250])
        c_nv.set("title", alt[:80])

    def _content_slide(self, s: Slide, n: int):
        slide = self._slide(self.p.light)
        self._title(slide, s.title, self.p.primary)
        self._rect(slide, (L.MX, 1.68, 0.9, 0.07), self.p.accent1, name="Title accent")
        self._footer(slide, n, dark=False)
        self._notes(slide, s.notes)
        if s.footnote:
            box = (L.MX, L.FOOTNOTE_Y, L.CONTENT_W, L.FOOTNOTE_H)
            self._text(
                slide, box, [s.footnote], CAPTION, self.p.muted, italic=True, name="Footnote"
            )
        return slide

    def _dark_slide(self, s: Slide, n: int, footer: bool = True):
        slide = self._slide(self.p.dark)
        self._notes(slide, s.notes)
        if footer:
            self._footer(slide, n, dark=True)
        return slide

    def _image_for(self, s: Slide) -> bytes | None:
        if s.part > 1:
            return None
        return self.images.get("hero" if s.layout == "title" else s.uid)

    # --- layout'ы ---------------------------------------------------------------------------

    def _title_art(self, slide) -> None:
        """Титульный слайд без картинки (или если генерация не удалась): слоистые плашки палитры
        справа — композиция, а не «дырка на месте фото»."""
        p = self.p
        hx, _, hw, hh = L.HERO_BOX
        self._rect(slide, L.HERO_BOX, p.dark, name="Panel")
        self._rect(slide, (hx, 0, 0.14, hh), p.accent1, name="Panel accent")
        tint = lambda t: mix(p.primary, "FFFFFF", t)
        slabs = (  # (ширина, цвет) — от правого края, сверху вниз: только primary, его тона и акцент
            (hw - 0.55, tint(0.45)),
            (hw - 1.6, p.primary),
            (hw - 2.6, tint(0.7)),
            (hw - 3.5, p.accent1),
        )
        for i, (w, color) in enumerate(slabs):
            self._rect(
                slide, (L.SLIDE_W - w, 1.0 + i * 1.5, w, 1.05), color, name=f"Panel slab {i + 1}"
            )
        self._rect(slide, (hx + 0.55, 6.55, 0.9, 0.09), p.accent1, name="Panel mark")

    def _r_title(self, s: Slide, n: int) -> None:
        slide = self._dark_slide(s, n, footer=False)
        p = self.p
        hero = self.images.get("hero")
        if hero:
            self._picture(slide, hero, L.HERO_BOX, self.deck.hero_prompt or s.title, "Hero image")
        else:
            self._title_art(slide)
        title_pt, shown = L.title_fit(s.title, L.TITLE_TEXT_W, 2.4)
        title_h = L.block_height([shown], title_pt, L.TITLE_TEXT_W, bold=True)
        sub_size, sub_h = BODY_FLOOR, 0.0
        if s.subtitle:
            sub_size = L.fit_size([s.subtitle], L.TITLE_TEXT_W, 1.5, 22, BODY_FLOOR) or BODY_FLOOR
            sub_h = L.block_height([s.subtitle], sub_size, L.TITLE_TEXT_W)
        group = title_h + (0.4 + sub_h if s.subtitle else 0.0)
        top = max(1.6, (L.SLIDE_H - group) / 2 + 0.1)
        self._rect(slide, (0.7, top - 0.4, 0.9, 0.09), p.accent1, name="Accent")
        self._title(
            slide,
            s.title,
            p.text_on_dark,
            (0.7, top, L.TITLE_TEXT_W, title_h + 0.05),
            MSO_ANCHOR.TOP,
        )
        if s.subtitle:
            sub_box = (0.7, top + title_h + 0.4, L.TITLE_TEXT_W, sub_h + 0.05)
            self._text(slide, sub_box, [s.subtitle], sub_size, p.muted_on_dark, name="Subtitle")

    def _r_section(self, s: Slide, n: int) -> None:
        slide = self._dark_slide(s, n)
        p = self.p
        x, w = L.MX + 0.55, L.CONTENT_W - 0.55
        top = 1.6
        num_h = L.line_height(88)
        self._text(
            slide,
            (x, top, 4.0, num_h),
            [f"{s.number or n:02d}"],
            88,
            p.accent_on_dark,
            bold=True,
            name="Section number",
        )
        title_pt, shown = L.title_fit(s.title, w, 1.4)
        title_h = L.block_height([shown], title_pt, w, bold=True)
        ty = top + num_h + 0.15
        self._title(slide, s.title, p.text_on_dark, (x, ty, w, title_h + 0.05), MSO_ANCHOR.TOP)
        bottom = ty + title_h
        if s.subtitle:
            size = L.fit_size([s.subtitle], w, 1.2, 22, BODY_FLOOR) or BODY_FLOOR
            sub_h = L.block_height([s.subtitle], size, w)
            self._text(
                slide,
                (x, bottom + 0.3, w, sub_h),
                [s.subtitle],
                size,
                p.muted_on_dark,
                name="Subtitle",
            )
            bottom += 0.3 + sub_h
        self._rect(slide, (L.MX, top + 0.1, 0.14, bottom - top - 0.1), p.accent1, name="Accent")

    def _bullet_block(
        self,
        slide,
        items,
        box,
        color,
        *,
        accent_bar=True,
        bar_x=None,
        max_pt=None,
        bullet_color=None,
    ):
        """Список с маркерами: кегль до 26 pt, интервал между пунктами растёт, пока блок не займёт
        ~70% области; блок центрируется по вертикали, акцентная полоса == высоте текста."""
        if max_pt is None:  # до 4 пунктов — 28 pt, 5 — 24 pt (ниже — только если не помещается)
            max_pt = BULLET_MAX_SHORT if len(items) <= 4 else BULLET_MAX_FIVE
        size = L.fit_size(items, box[2], box[3], max_pt, BODY_FLOOR, bullet=True) or BODY_FLOOR
        lines_h = sum(
            len(L.wrap_lines(t, size, box[2] - L.BULLET_INDENT)) * L.line_height(size)
            for t in items
        )
        n = len(items)
        gap_pt = size * 0.5
        if n > 1:
            want = (box[3] * 0.72 - lines_h) / (n - 1) * 72
            room = (box[3] - lines_h) / (n - 1) * 72
            gap_pt = max(size * 0.5, min(size * 2.2, want, room))
        height = lines_h + gap_pt / 72 * max(0, n - 1)
        top = box[1] + max(0.0, box[3] - height) * 0.45
        if accent_bar and items:
            x = bar_x if bar_x is not None else L.MX
            self._rect(slide, (x, top, 0.07, height), self.p.accent1, name="Bullets accent")
        self._text(
            slide,
            (box[0], top, box[2], height + 0.02),
            items,
            size,
            color,
            bullet=bullet_color or self.p.accent1,
            gap=gap_pt,
            name="Bullets",
        )

    def _r_bullets(self, s: Slide, n: int) -> None:
        slide = self._content_slide(s, n)
        image = self._image_for(s)
        box = L.bullets_box(bool(image))
        if image:
            self._picture(slide, image, L.SIDE_IMG_BOX, s.image_prompt or s.title, "Picture")
        self._bullet_block(slide, s.bullets, box, self.p.text)

    def _r_image(self, s: Slide, n: int) -> None:
        slide = self._content_slide(s, n)
        image = self._image_for(s)
        if not image:  # без картинки — обычный список на всю ширину
            self._bullet_block(slide, s.bullets, L.bullets_box(False), self.p.text)
            return
        self._picture(slide, image, L.WIDE_IMG_BOX, s.image_prompt or s.title, "Picture")
        box = L.image_slide_text_box()
        self._bullet_block(slide, s.bullets, box, self.p.text, accent_bar=False, max_pt=24)

    def _r_cards(self, s: Slide, n: int) -> None:
        slide = self._content_slide(s, n)
        p = self.p
        bars = [p.accent1, p.accent2, p.primary, p.accent1, p.accent2]
        geom = L.cards_geometry(s.cards, s.variant or "grid")
        for i, ((title, text), box, (tbox, xbox)) in enumerate(
            zip(s.cards, geom.boxes, geom.texts, strict=True)
        ):
            bar = bars[i % len(bars)]
            self._rect(slide, box, p.surface, name="Card")
            if geom.variant == "grid":
                self._rect(slide, (box[0], box[1], box[2], L.CARD_BAR), bar, name="Card bar")
            else:
                d = L.NUM_BADGE
                badge = self._rect(
                    slide,
                    (L.MX + 0.3, box[1] + (box[3] - d) / 2, d, d),
                    bar,
                    shape=MSO_SHAPE.OVAL,
                    name=f"Card number {i + 1}",
                )
                self._badge_text(badge, str(i + 1), best_text_on(bar), 22)
            if tbox:
                self._text(
                    slide,
                    tbox,
                    [title],
                    geom.title_pt,
                    p.primary,
                    bold=True,
                    anchor=MSO_ANCHOR.MIDDLE if geom.variant == "numbered" else MSO_ANCHOR.TOP,
                    name="Card title",
                )
            if xbox:
                self._text(
                    slide,
                    xbox,
                    [text],
                    geom.size,
                    p.text,
                    anchor=MSO_ANCHOR.MIDDLE if geom.variant == "numbered" else MSO_ANCHOR.TOP,
                    name="Card text",
                )

    @staticmethod
    def _badge_text(shape, text: str, color: str, size: float) -> None:
        tf = shape.text_frame
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        para = tf.paragraphs[0]
        para.alignment = PP_ALIGN.CENTER
        run = para.add_run()
        run.text = text
        DeckRenderer._style_run(run, size, color, bold=True)

    @staticmethod
    def _split_value(value: str) -> tuple[str, str]:
        """«124 млн ₸» -> («124», «млн ₸»); «+18%» -> («+18», «%»). Единица рисуется мельче."""
        m = _STAT_SPLIT.match(value.strip())
        if not m:
            return value.strip(), ""
        return m.group(1).strip(), m.group(2).strip()

    def _r_stat(self, s: Slide, n: int) -> None:
        slide = self._content_slide(s, n)
        p = self.p
        cols = L.step_columns(len(s.stats))
        parts = [self._split_value(v) for v, _ in s.stats]
        pad = L.CARD_PAD
        inner_w = cols[0][1] - 2 * pad

        def unit_pt(pt: int) -> int:
            return max(18, round(pt * 0.5))

        def width_of(num: str, unit: str, pt: int) -> float:
            sep = "" if unit.startswith(("%", "‰")) or not unit else THIN
            return L.text_width(num, pt, True) + (
                L.text_width(sep + unit, unit_pt(pt), True) if unit else 0.0
            )

        # один кегль для всех значений слайда: наименьший из подобранных
        pt = 60
        while pt > 24 and any(width_of(num, unit, pt) > inner_w * 0.96 for num, unit in parts):
            pt -= 2
        labels = [lab for _, lab in s.stats]
        lsize = L.fit_size(labels, inner_w, 1.5, 22, BODY_FLOOR) or BODY_FLOOR
        label_h = max(L.block_height([lab], lsize, inner_w) for lab in labels)
        value_h = L.line_height(pt) + 0.1
        panel_h = L.CARD_BAR + 0.3 + value_h + 0.2 + label_h + 0.35  # строго по содержимому
        bullets = s.bullets[:2]
        b_size = (
            L.fit_size(bullets, L.CONTENT_W - 0.3, 1.4, 22, BODY_FLOOR, bullet=True) or BODY_FLOOR
        )
        b_h = (
            (L.block_height(bullets, b_size, L.CONTENT_W - 0.3, bullet=True) + 0.02)
            if bullets
            else 0.0
        )
        group_h = panel_h + (0.4 + b_h if bullets else 0.0)
        top = L.centered_top(group_h, bias=0.5)
        for (num, unit), (_, label), (x, w) in zip(parts, s.stats, cols, strict=True):
            self._rect(slide, (x, top, w, panel_h), p.surface, name="Stat card")
            self._rect(slide, (x, top, w, L.CARD_BAR), p.accent1, name="Stat accent")
            vy = top + L.CARD_BAR + 0.3
            tb = self._text(
                slide,
                (x + pad, vy, inner_w, value_h),
                [num],
                pt,
                p.primary,
                bold=True,
                name="Stat value",
            )
            if unit:
                run = tb.text_frame.paragraphs[0].add_run()
                run.text = ("" if unit.startswith(("%", "‰")) else THIN) + unit
                self._style_run(run, unit_pt(pt), p.primary, bold=True)
            self._text(
                slide,
                (x + pad, vy + value_h + 0.2, inner_w, label_h),
                [label],
                lsize,
                p.muted,
                name="Stat label",
            )
        if bullets:
            box = (L.MX + 0.3, top + panel_h + 0.4, L.CONTENT_W - 0.3, b_h)
            self._bullet_block(slide, bullets, box, p.text, max_pt=22)

    def _r_two_column(self, s: Slide, n: int) -> None:
        slide = self._content_slide(s, n)
        lbox, rbox = L.column_text_boxes()
        inner_w, inner_h = lbox[2] - 2 * L.CARD_PAD - 0.1, lbox[3] - 2 * L.CARD_PAD
        cols = [c for c in (s.left, s.right) if c]
        size = min(
            (
                L.fit_size(c, inner_w, inner_h, BODY_MAX, BODY_FLOOR, bullet=True) or BODY_FLOOR
                for c in cols
            ),
            default=BODY_FLOOR,
        )
        need = max((L.block_height(c, size, inner_w, bullet=True) for c in cols), default=1.0)
        gap_pt = size * 0.9 if len(max(cols, key=len, default=[])) > 1 else size * 0.5
        need += (gap_pt - size * 0.5) / 72 * max(0, max((len(c) for c in cols), default=1) - 1)
        h = min(L.CONTENT_H, max(3.4, need + 2 * L.CARD_PAD + 0.2))
        top = L.centered_top(h)
        for items, box, accent in ((s.left, lbox, self.p.accent1), (s.right, rbox, self.p.accent2)):
            if not items:
                continue
            panel = (box[0], top, box[2], h)
            self._rect(slide, panel, self.p.surface, name="Panel")
            self._rect(slide, (panel[0], panel[1], 0.09, h), accent, name="Panel accent")
            inner = (panel[0] + L.CARD_PAD + 0.1, top + L.CARD_PAD, inner_w, h - 2 * L.CARD_PAD)
            self._text(
                slide,
                inner,
                items,
                size,
                self.p.text,
                bullet=accent_text_safe(self.p),
                gap=gap_pt,
                name="Column",
            )

    def _r_comparison(self, s: Slide, n: int) -> None:
        slide = self._content_slide(s, n)
        p = self.p
        titles = [t for t, _ in s.cards[:2]]
        bodies = [s.left or [""], s.right or [""]]
        g = L.comparison_geometry(titles, bodies)
        inner_w = g.panels[0][2] - 2 * L.CARD_PAD
        for title, lines, panel, head in zip(titles, bodies, g.panels, g.heads, strict=True):
            self._rect(slide, panel, p.surface, name="Panel")
            self._rect(
                slide, (panel[0], panel[1], panel[2], L.CMP_HEAD_H), p.primary, name="Panel head"
            )
            self._text(
                slide,
                head,
                [title],
                g.title_pt,
                best_text_on(p.primary),
                bold=True,
                anchor=MSO_ANCHOR.MIDDLE,
                name="Panel title",
            )
            body_h = L.block_height(lines, g.size, inner_w, bullet=True) + (
                (g.gap_pt - g.size * 0.5) / 72 * max(0, len(lines) - 1)
            )
            inner = (panel[0] + L.CARD_PAD, panel[1] + L.CMP_HEAD_H + 0.3, inner_w, body_h + 0.02)
            self._text(
                slide,
                inner,
                lines,
                g.size,
                p.text,
                bullet=p.accent1,
                gap=g.gap_pt,
                name="Panel text",
            )
        cx = L.SLIDE_W / 2
        d = L.VS_D
        vs = self._rect(
            slide,
            (cx - d / 2, g.top + L.CMP_HEAD_H / 2 - d / 2, d, d),
            p.accent1,
            shape=MSO_SHAPE.OVAL,
            name="VS",
        )
        self._badge_text(vs, "VS", best_text_on(p.accent1), 16)

    def _r_timeline(self, s: Slide, n: int) -> None:
        slide = self._content_slide(s, n)
        p = self.p
        g = L.timeline_geometry(s.steps, s.variant or "h")
        for box in g.lines:
            self._rect(slide, box, mix(p.primary, "FFFFFF", 0.65), name="Timeline")
        for i, ((title, text), dot, tb, xb) in enumerate(
            zip(s.steps, g.dots, g.titles, g.texts, strict=True), 1
        ):
            shape = self._rect(slide, dot, p.primary, shape=MSO_SHAPE.OVAL, name=f"Step {i}")
            self._badge_text(shape, str(i), best_text_on(p.primary), 20)
            vert = g.variant == "v"
            anchor = MSO_ANCHOR.MIDDLE if vert else MSO_ANCHOR.TOP
            self._text(
                slide,
                tb,
                [title],
                g.title_pt,
                p.primary,
                bold=True,
                anchor=anchor,
                name="Step title",
            )
            if text:
                self._text(slide, xb, [text], g.body_pt, p.text, anchor=anchor, name="Step text")

    def _r_table(self, s: Slide, n: int) -> None:
        slide = self._content_slide(s, n)
        p = self.p
        table = s.table
        assert table is not None
        for pt in (22, 20, 18, 16, 15, 14):
            widths, heights = L.table_geometry(table, pt)
            if sum(heights) <= L.CONTENT_H * 0.88:
                break
        target = min(L.CONTENT_H * 0.62, len(heights) * 0.95)  # короткая таблица не должна «тонуть»
        if sum(heights) < target:
            heights = [h * target / sum(heights) for h in heights]
        all_rows = ([table.headers] if table.headers else []) + table.rows
        ncols = len(widths)
        gf = slide.shapes.add_table(
            len(all_rows),
            ncols,
            Inches(L.MX),
            Inches(L.CONTENT_Y + max(0.0, L.CONTENT_H - sum(heights)) * 0.3),
            Inches(L.CONTENT_W),
            Inches(sum(heights)),
        )
        gf.name = "Table"
        tbl = gf.table
        tbl.first_row = bool(table.headers)
        tbl.horz_banding = False
        for c, w in enumerate(widths):
            tbl.columns[c].width = Inches(w)
        for r, row in enumerate(all_rows):
            tbl.rows[r].height = Inches(heights[r])
            header = bool(table.headers) and r == 0
            body_idx = r - (1 if table.headers else 0)
            fill = p.primary if header else (p.surface if body_idx % 2 == 0 else "FFFFFF")
            color = best_text_on(p.primary) if header else p.text
            for c in range(ncols):
                cell = tbl.cell(r, c)
                text = row[c] if c < len(row) else ""
                cell.fill.solid()
                cell.fill.fore_color.rgb = _rgb(fill)
                cell.margin_left = cell.margin_right = Inches(0.12)
                cell.margin_top = cell.margin_bottom = Inches(0.08)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                tf = cell.text_frame
                tf.word_wrap = True
                para = tf.paragraphs[0]
                para.alignment = (
                    PP_ALIGN.RIGHT
                    if (not header and _NUMERIC.match(text or "x"))
                    else PP_ALIGN.LEFT
                )
                run = para.add_run()
                run.text = text
                self._style_run(run, pt, color, bold=header)

    def _r_chart(self, s: Slide, n: int) -> None:
        slide = self._content_slide(s, n)
        p = self.p
        spec = s.chart
        assert spec is not None
        wide = round(L.CONTENT_W * 0.62, 2) if s.bullets else L.CONTENT_W
        box = (L.MX, L.CONTENT_Y, wide, L.CONTENT_H)
        data = CategoryChartData()
        data.categories = spec.categories
        for name, values in spec.series:
            data.add_series(name, values)
        kind = {
            "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
            "line": XL_CHART_TYPE.LINE_MARKERS,
            "pie": XL_CHART_TYPE.PIE,
        }[spec.type]
        gf = slide.shapes.add_chart(kind, *self._emu(box), data)
        gf.name = "Chart"
        chart = gf.chart
        chart.font.size = Pt(14)
        chart.font.name = FONT
        chart.font.color.rgb = _rgb(p.text)
        pie = spec.type == "pie"
        chart.has_title = False
        if pie and spec.unit:  # доли в процентах, единицу исходных данных — в заголовок
            chart.has_title = True
            chart.chart_title.include_in_layout = False
            chart.chart_title.text_frame.text = f"{spec.series[0][0]}, {spec.unit}"
            run = chart.chart_title.text_frame.paragraphs[0].runs[0]
            self._style_run(run, 16, p.muted, bold=False)
        chart.has_legend = pie or len(spec.series) > 1  # один ряд -> без легенды
        if chart.has_legend:
            chart.legend.position = XL_LEGEND_POSITION.RIGHT if pie else XL_LEGEND_POSITION.BOTTOM
            chart.legend.include_in_layout = False
            chart.legend.font.size = Pt(16 if pie else 14)
        colors = p.chart_colors
        plot = chart.plots[0]
        all_values = [v for _, vals in spec.series for v in vals]
        plot.has_data_labels = True
        labels = plot.data_labels
        labels.font.size = Pt(14)
        labels.font.name = FONT
        if pie:
            labels.show_percentage = True
            labels.show_value = False
            labels.show_category_name = False  # названия — в легенде справа
            labels.number_format, labels.number_format_is_linked = "0%", False
            labels.position = XL_LABEL_POSITION.OUTSIDE_END
            for i in range(len(spec.categories)):
                pt = plot.series[0].points[i]
                pt.format.fill.solid()
                pt.format.fill.fore_color.rgb = _rgb(colors[i % len(colors)])
            return self._chart_takeaways(slide, s, wide)
        zero = spec.type == "bar" or min(all_values) <= 0.5 * max(max(all_values), 1e-9)
        lo, hi, step = L.nice_axis(min(all_values), max(all_values), zero)
        all_int = all(float(v).is_integer() for v in all_values)
        number_format = (
            "#,##0" if all_int and step >= 1 else ("#,##0.0" if step >= 0.1 else "#,##0.00")
        )
        labels.number_format, labels.number_format_is_linked = number_format, False
        labels.position = (
            XL_LABEL_POSITION.OUTSIDE_END if spec.type == "bar" else XL_LABEL_POSITION.ABOVE
        )
        if spec.type == "bar":
            plot.gap_width = 60
        for i, ser in enumerate(plot.series):
            color = _rgb(colors[i % len(colors)])
            if spec.type == "bar":
                ser.format.fill.solid()
                ser.format.fill.fore_color.rgb = color
            else:
                ser.format.line.color.rgb = color
                ser.format.line.width = Pt(3)
                ser.smooth = False
                ser.marker.format.fill.solid()
                ser.marker.format.fill.fore_color.rgb = color
                ser.marker.format.line.color.rgb = color
        val = chart.value_axis
        val.minimum_scale, val.maximum_scale, val.major_unit = lo, hi, step
        val.has_major_gridlines = True
        val.major_gridlines.format.line.color.rgb = _rgb(mix(p.light, p.muted, 0.2))
        val.format.line.fill.background()
        val.tick_labels.font.size = Pt(14)
        val.tick_labels.number_format, val.tick_labels.number_format_is_linked = (
            number_format,
            False,
        )
        if spec.unit:  # единица измерения — в названии оси значений
            val.has_title = True
            val.axis_title.text_frame.text = spec.unit
            self._style_run(val.axis_title.text_frame.paragraphs[0].runs[0], 14, p.muted)
        cat = chart.category_axis
        cat.tick_labels.font.size = Pt(14)
        cat.format.line.color.rgb = _rgb(mix(p.light, p.muted, 0.4))
        self._chart_takeaways(slide, s, wide)

    def _chart_takeaways(self, slide, s: Slide, wide: float) -> None:
        """2-3 вывода справа от диаграммы (~38% ширины)."""
        if not s.bullets:
            return
        bx = L.MX + wide + L.GAP
        box = (bx + 0.3, L.CONTENT_Y, L.SLIDE_W - L.MX - bx - 0.3, L.CONTENT_H)
        self._bullet_block(slide, s.bullets, box, self.p.text, bar_x=bx, max_pt=22)

    def _r_quote(self, s: Slide, n: int) -> None:
        slide = self._content_slide(s, n)
        p = self.p
        assert s.quote is not None
        text, author = s.quote
        width = L.CONTENT_W - 1.6
        size = L.fit_size([text], width, L.CONTENT_H - 1.4, 32, BODY_FLOOR) or BODY_FLOOR
        th = L.block_height([text], size, width)
        block = th + (0.4 + 0.45 if author else 0.0)
        top = L.centered_top(block, bias=0.35)
        self._text(
            slide,
            (L.MX, top - 0.15, 1.3, 2.1),
            ["“"],
            120,
            p.primary,
            bold=True,
            name="Quote mark",
        )
        self._rect(slide, (L.MX + 1.2, top, 0.07, block), p.accent1, name="Quote accent")
        self._text(
            slide,
            (L.MX + 1.5, top, width - 0.2, th),
            [text],
            size,
            p.text,
            italic=True,
            name="Quote",
        )
        if author:
            self._text(
                slide,
                (L.MX + 1.5, top + th + 0.4, width - 0.2, 0.45),
                [f"— {author}"],
                20,
                p.muted,
                name="Quote author",
            )

    def _r_closing(self, s: Slide, n: int) -> None:
        slide = self._dark_slide(s, n)
        p = self.p
        self._rect(slide, (L.MX, 1.0, 0.9, 0.09), p.accent1, name="Accent")
        self._title(slide, s.title, p.text_on_dark, (L.MX, 1.25, L.CONTENT_W, 1.3), MSO_ANCHOR.TOP)
        items = s.bullets
        if s.subtitle and not items:
            items = [s.subtitle]
        if items:
            box = (L.MX + 0.3, 2.9, L.CONTENT_W - 0.3, 3.7)
            self._bullet_block(
                slide,
                items,
                box,
                p.text_on_dark,
                accent_bar=False,
                bullet_color=p.accent_on_dark,
            )

    # --- сборка ---------------------------------------------------------------------------------

    def build(self) -> RenderResult:
        for n, s in enumerate(self.slides, 1):
            getattr(self, f"_r_{s.layout}")(s, n)
        out = io.BytesIO()
        self.prs.save(out)
        return RenderResult(out.getvalue(), len(self.slides))


def accent_text_safe(p: Palette) -> str:
    """Цвет маркера списка на светлой карточке (маркер — декор, не текст)."""
    return p.accent1


def render_deck(deck: Deck, slides: list[Slide], images: dict) -> RenderResult:
    return DeckRenderer(deck, slides, images).build()


__all__ = ["RenderResult", "prepare_picture", "render_deck"]
