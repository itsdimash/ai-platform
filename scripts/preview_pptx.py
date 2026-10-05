# ruff: noqa: S110, RUF059, BLE001
"""Приближённый предпросмотр .pptx без офисного рендерера (для разработки и проверки вёрстки).

    uv run python scripts/preview_pptx.py deck.pptx [out_dir]

Читает готовый файл python-pptx и рисует каждый слайд в PNG (100 px на дюйм) + общий PDF:
фоны, фигуры, текст (перенос по тем же метрикам, что у планировщика, шрифт DejaVu),
картинки, таблицы и упрощённые диаграммы. Текст, не помещающийся в свою рамку, обводится
красным. Это НЕ PowerPoint/LibreOffice: реальные шрифты, переносы и диаграммы будут
отличаться — настоящий рендер: soffice --headless --convert-to pdf.
"""

import io
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from app.utils.deck import layout as L

DPI = 100
EMU = 914400
FONT_DIR = Path(__file__).resolve().parent.parent / "app" / "assets" / "fonts"
NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def font(pt: float, bold=False, italic=False) -> ImageFont.FreeTypeFont:
    name = "Carlito-Bold.ttf" if bold else "Carlito-Regular.ttf"  # метрики Calibri
    return ImageFont.truetype(str(FONT_DIR / name), max(6, round(pt * DPI / 72)))


def hex_rgb(h: str) -> tuple[int, int, int]:
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def fill_of(shape):
    try:
        if shape.fill.type == 1:
            return hex_rgb(str(shape.fill.fore_color.rgb))
    except Exception:
        pass
    return None


def draw_text(d: ImageDraw.ImageDraw, shape, box, default_color=(30, 30, 30)) -> bool:
    """Рисует текст формы в рамку; возвращает True, если текст не поместился."""
    x, y, w, h = box
    tf = shape.text_frame
    paras = []
    for para in tf.paragraphs:
        runs = [r for r in para.runs if r.text]
        if not runs and para._p.find(NS_A + "fld") is None:
            continue
        size = next((r.font.size.pt for r in runs if r.font.size), 14)
        bold = any(r.font.bold for r in runs)
        italic = any(r.font.italic for r in runs)
        color = default_color
        for r in runs:
            try:
                color = hex_rgb(str(r.font.color.rgb))
                break
            except Exception:
                pass
        text = "".join(r.text for r in runs)
        fld = para._p.find(NS_A + "fld")
        if fld is not None:
            text = fld.findtext(NS_A + "t") or ""
            srgb = fld.find(f"{NS_A}rPr/{NS_A}solidFill/{NS_A}srgbClr")
            if srgb is not None:
                color = hex_rgb(srgb.get("val"))
        bullet = para._p.pPr is not None and para._p.pPr.find(NS_A + "buChar") is not None
        bcolor = color
        if bullet:
            clr = para._p.pPr.find(f"{NS_A}buClr/{NS_A}srgbClr")
            if clr is not None:
                bcolor = hex_rgb(clr.get("val"))
        parts = [(r.text, r.font.size.pt if r.font.size else size, bool(r.font.bold)) for r in runs]
        multi = len({sz for _, sz, _ in parts}) > 1  # число + мелкая единица: одна строка
        if multi:
            size = max(sz for _, sz, _ in parts)
        after = para.space_after.pt / 72 if para.space_after is not None else None
        paras.append(
            (
                text,
                size,
                bold,
                italic,
                color,
                bullet,
                bcolor,
                para.alignment,
                parts if multi else None,
                after,
            )
        )
    anchor = tf.vertical_anchor
    total = 0.0
    for i, (text, size, bold, _i, _c, bullet, _b, _al, multi, _after) in enumerate(paras):
        ww = w - (L.BULLET_INDENT if bullet else 0)
        n_lines = 1 if multi else len(L.wrap_lines(text, size, ww, bold))
        prev_after = paras[i - 1][9] if i else None
        total += n_lines * L.line_height(size) + (
            (prev_after if prev_after is not None else L.para_gap(size)) if i else 0
        )
    cy = y
    if anchor is not None and int(anchor) == 3:  # MIDDLE
        cy = y + (h - total) / 2
    elif anchor is not None and int(anchor) == 4:  # BOTTOM
        cy = y + h - total
    for i, (text, size, bold, italic, color, bullet, bcolor, align, multi, after) in enumerate(
        paras
    ):
        ww = w - (L.BULLET_INDENT if bullet else 0)
        f = font(size, bold, italic)
        if multi:
            tx = x
            for rtext, rsize, rbold in multi:
                ry = cy + (L.line_height(size) - L.line_height(rsize)) * 0.8
                d.text((tx * DPI, ry * DPI), rtext, font=font(rsize, rbold, italic), fill=color)
                tx += L.text_width(rtext, rsize, rbold)
            cy += L.line_height(size) + (
                (after if after is not None else L.para_gap(size)) if i < len(paras) - 1 else 0
            )
            continue
        lines = L.wrap_lines(text, size, ww, bold)
        if bullet:
            r = size * DPI / 72 * 0.14
            by = (cy + L.line_height(size) * 0.55) * DPI
            d.ellipse([(x + 0.08) * DPI - r, by - r, (x + 0.08) * DPI + r, by + r], fill=bcolor)
        for line in lines:
            lw = L.text_width(line, size, bold)
            if align is not None and int(align) == 2:  # CENTER
                tx = x + (ww - lw) / 2
            elif align is not None and int(align) == 3:  # RIGHT
                tx = x + ww - lw
            else:
                tx = x + (L.BULLET_INDENT if bullet else 0)
            d.text((tx * DPI, cy * DPI), line, font=f, fill=color)
            cy += L.line_height(size)
        if i < len(paras) - 1:
            cy += after if after is not None else L.para_gap(size)
    over = total > h * 1.04 + 0.03
    if over:
        d.rectangle([x * DPI, y * DPI, (x + w) * DPI, (y + h) * DPI], outline=(255, 0, 0), width=3)
    return over


def draw_chart(d, shape, box, colors=None):
    x, y, w, h = box
    chart = shape.chart
    plot = chart.plots[0]
    cats = list(plot.categories)
    series = [(s.name, list(s.values)) for s in plot.series]
    palette = [
        (31, 58, 95),
        (242, 169, 0),
        (92, 141, 199),
        (120, 150, 190),
        (200, 140, 0),
        (60, 100, 150),
    ]
    px, py, pw, ph = x * DPI + 50, y * DPI + 30, w * DPI - 80, h * DPI - 110
    d.rectangle([x * DPI, y * DPI, (x + w) * DPI, (y + h) * DPI], outline=(210, 210, 210))
    f = font(12)
    kind = str(chart.chart_type)
    if "PIE" in kind:
        total = sum(series[0][1]) or 1
        cx, cy, r = px + pw / 2, py + ph / 2, min(pw, ph) / 2.2
        start = -90
        for i, v in enumerate(series[0][1]):
            ang = v / total * 360
            d.pieslice([cx - r, cy - r, cx + r, cy + r], start, start + ang, fill=palette[i % 6])
            mid = math.radians(start + ang / 2)
            d.text(
                (cx + (r + 25) * math.cos(mid) - 12, cy + (r + 25) * math.sin(mid) - 8),
                f"{v / total:.0%}",
                font=f,
                fill=(30, 30, 30),
            )
            start += ang
        return
    vmax = max(max(v) for _, v in series) * 1.15 or 1
    n, m = len(cats), len(series)
    slot = pw / max(n, 1)
    d.line([px, py + ph, px + pw, py + ph], fill=(120, 120, 120))
    for gi in range(1, 5):
        gy = py + ph - ph * gi / 4
        d.line([px, gy, px + pw, gy], fill=(225, 225, 225))
        d.text((x * DPI + 4, gy - 8), f"{vmax * gi / 4:.0f}", font=f, fill=(90, 90, 90))
    for ci, c in enumerate(cats):
        d.text(
            (px + slot * ci + slot / 2 - len(str(c)) * 4, py + ph + 6),
            str(c),
            font=f,
            fill=(30, 30, 30),
        )
        for si, (_n, vals) in enumerate(series):
            val = vals[ci]
            if "LINE" in kind:
                pts = [(px + slot * k + slot / 2, py + ph - ph * vals[k] / vmax) for k in range(n)]
                if ci == 0:
                    d.line(pts, fill=palette[si % 6], width=4)
                cxp, cyp = pts[ci]
                d.ellipse([cxp - 5, cyp - 5, cxp + 5, cyp + 5], fill=palette[si % 6])
            else:
                bw = slot * 0.7 / m
                bx = px + slot * ci + slot * 0.15 + bw * si
                top = py + ph - ph * val / vmax
                d.rectangle([bx, top, bx + bw, py + ph], fill=palette[si % 6])
                d.text((bx, top - 16), f"{val:g}", font=f, fill=(30, 30, 30))
    if len(series) > 1:
        lx = px
        for si, (nm, _v) in enumerate(series):
            d.rectangle(
                [lx, y * DPI + h * DPI - 24, lx + 14, y * DPI + h * DPI - 10], fill=palette[si % 6]
            )
            d.text((lx + 20, y * DPI + h * DPI - 26), str(nm), font=f, fill=(30, 30, 30))
            lx += 110


def draw_table(d, shape, box):
    x, y, w, h = box
    tbl = shape.table
    cy = y
    for r, row in enumerate(tbl.rows):
        cx = x
        rh = row.height / EMU
        for c, col in enumerate(tbl.columns):
            cw = col.width / EMU
            cell = tbl.cell(r, c)
            fill = (255, 255, 255)
            try:
                fill = hex_rgb(str(cell.fill.fore_color.rgb))
            except Exception:
                pass
            d.rectangle(
                [cx * DPI, cy * DPI, (cx + cw) * DPI, (cy + rh) * DPI],
                fill=fill,
                outline=(220, 220, 220),
            )
            runs = [rn for p in cell.text_frame.paragraphs for rn in p.runs]
            if runs:
                size = runs[0].font.size.pt if runs[0].font.size else 14
                try:
                    color = hex_rgb(str(runs[0].font.color.rgb))
                except Exception:
                    color = (30, 30, 30)
                bold = bool(runs[0].font.bold)
                lines = L.wrap_lines("".join(rn.text for rn in runs), size, cw - 0.24, bold)
                align = cell.text_frame.paragraphs[0].alignment
                ty = cy + (rh - len(lines) * L.line_height(size)) / 2
                for line in lines:
                    lw = L.text_width(line, size, bold)
                    tx = cx + cw - 0.12 - lw if align is not None and int(align) == 3 else cx + 0.12
                    d.text((tx * DPI, ty * DPI), line, font=font(size, bold), fill=color)
                    ty += L.line_height(size)
            cx += cw
        cy += rh


def render_slide(slide, prs) -> tuple[Image.Image, int]:
    W, H = round(prs.slide_width / EMU * DPI), round(prs.slide_height / EMU * DPI)
    bg = (255, 255, 255)
    try:
        bg = hex_rgb(str(slide.background.fill.fore_color.rgb))
    except Exception:
        pass
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    overflow = 0
    for shape in slide.shapes:
        box = (shape.left / EMU, shape.top / EMU, shape.width / EMU, shape.height / EMU)
        x, y, w, h = box
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            pic = (
                Image.open(io.BytesIO(shape.image.blob))
                .convert("RGB")
                .resize((round(w * DPI), round(h * DPI)))
            )
            im.paste(pic, (round(x * DPI), round(y * DPI)))
            continue
        if getattr(shape, "has_chart", False) and shape.has_chart:
            draw_chart(d, shape, box)
            continue
        if getattr(shape, "has_table", False) and shape.has_table:
            draw_table(d, shape, box)
            continue
        rect = [x * DPI, y * DPI, (x + w) * DPI, (y + h) * DPI]
        f = fill_of(shape) if shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE else None
        if f is not None:
            if "OVAL" in str(shape.auto_shape_type):
                d.ellipse(rect, fill=f)
            else:
                d.rectangle(rect, fill=f)
        if (
            shape.has_text_frame
            and shape.text_frame.text.strip()
            or (shape.has_text_frame and shape.text_frame._txBody.find(f".//{NS_A}fld") is not None)
        ):
            overflow += draw_text(d, shape, box)
    return im, overflow


def main() -> None:
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix("")
    out.mkdir(parents=True, exist_ok=True)
    prs = Presentation(str(src))
    pages = []
    total_overflow = 0
    for i, slide in enumerate(prs.slides, 1):
        im, over = render_slide(slide, prs)
        total_overflow += over
        im.save(out / f"slide_{i:02d}.png")
        pages.append(im)
    pages[0].save(out / f"{src.stem}.pdf", save_all=True, append_images=pages[1:], resolution=DPI)
    print(f"{len(pages)} слайдов -> {out}  (красная рамка = текст не помещается: {total_overflow})")


if __name__ == "__main__":
    main()
