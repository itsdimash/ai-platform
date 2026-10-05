"""Сетка слайда, оценка размера текста, подбор кегля и разбиение переполненных слайдов.

Размеры в дюймах (слайд 13,333 x 7,5, 16:9). Рендер (render.py) берёт геометрию и
кегли ОТСЮДА, поэтому планировщик и отрисовка не расходятся.

Ширина текста меряется шрифтом Carlito (метрически совместим с Calibri, лицензия OFL; лежит
в репозитории рядом с DejaVu). Коэффициент 1,02 — небольшой запас на кернинг/округления
рендереров. Если текст не помещается даже при 14 pt (пол для тела), слайд делится на два.
"""

import logging
import math
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

from app.limits import DECK_MAX_BULLETS, DECK_TABLE_ROWS_PER_SLIDE, MAX_SLIDES
from app.utils.deck.insights import chart_takeaways
from app.utils.deck.schema import Deck, Slide, Table, label_split
from app.utils.deck.themes import BODY_FLOOR, BODY_MAX, TITLE_MAX, TITLE_MIN

logger = logging.getLogger(__name__)

# --- Геометрия -----------------------------------------------------------------------------
SLIDE_W, SLIDE_H = 13.333, 7.5
MX = 0.6  # боковые поля (>= 0,5)
CONTENT_W = SLIDE_W - 2 * MX
TITLE_BOX = (MX, 0.45, CONTENT_W, 1.15)
CONTENT_Y = 1.95
CONTENT_BOTTOM = 6.55
CONTENT_H = CONTENT_BOTTOM - CONTENT_Y
FOOTER_Y, FOOTER_H = 7.02, 0.3
FOOTNOTE_Y, FOOTNOTE_H = 6.58, 0.22  # пометка (12 pt): >= 0,2 in над текстом футера
GAP = 0.3  # зазор между колонками/карточками

# Картинки
HERO_BOX = (8.2, 0.0, SLIDE_W - 8.2, SLIDE_H)  # титульный слайд: правая панель на всю высоту
TITLE_TEXT_W = 7.0
SIDE_IMG_W = 4.9  # bullets + картинка справа
SIDE_IMG_BOX = (SLIDE_W - MX - SIDE_IMG_W, CONTENT_Y, SIDE_IMG_W, CONTENT_H)
WIDE_IMG_BOX = (MX, CONTENT_Y, 6.3, CONTENT_H)  # layout image: картинка слева

WIDTH_FACTOR = 1.02
LINE_SPACING = (
    1.2  # точный межстрочный интервал (в кеглях): высота строки = pt * 1,2 / 72 в любом рендерере
)
BULLET_INDENT = 0.32

_FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"


@lru_cache(maxsize=4)
def _font(bold: bool) -> ImageFont.FreeTypeFont:
    name = "Carlito-Bold.ttf" if bold else "Carlito-Regular.ttf"
    return ImageFont.truetype(str(_FONT_DIR / name), 200)  # крупный кегль — точные метрики


@lru_cache(maxsize=200_000)
def _unit_width(text: str, bold: bool) -> float:
    return (
        _font(bold).getlength(text) / 200
    )  # ширина в «em»; кэш — планировщик меряет одни и те же слова


def text_width(text: str, pt: float, bold: bool = False) -> float:
    """Ширина строки в дюймах (оценка, см. модульную докстроку)."""
    return _unit_width(text, bold) * pt / 72 * WIDTH_FACTOR


def wrap_lines(text: str, pt: float, width: float, bold: bool = False) -> list[str]:
    """Жадный перенос по словам; слово длиннее строки режется по символам."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        tokens = words(paragraph)
        if not tokens:
            lines.append("")
            continue
        current = ""
        for word in tokens:
            candidate = f"{current} {word}".strip()
            if text_width(candidate, pt, bold) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
            while text_width(word, pt, bold) > width and len(word) > 1:
                k = len(word)
                while k > 1 and text_width(word[:k], pt, bold) > width:
                    k -= 1
                lines.append(word[:k])
                word = word[k:]
            current = word
        lines.append(current)
    return lines or [""]


def words(text: str) -> list[str]:
    """Слова для переноса: делим только по обычным пробелам — неразрывный пробел (число и единица
    измерения: «152 млн ₸») держит их вместе, как и настоящий рендерер."""
    return [w for w in re.split(r"[ \t\n]+", text) if w]


def longest_word_width(paras: list[str], pt: float, bold: bool = False) -> float:
    """Ширина самого длинного слова (дюймы): слова не должны рваться посреди слова."""
    return max((text_width(w, pt, bold) for t in paras for w in words(t)), default=0.0)


def centered_top(needed: float, avail: float = 0.0, top: float = 0.0, bias: float = 0.4) -> float:
    """Верх блока высотой needed, расположенного в области (top, avail) чуть выше центра."""
    avail = avail or CONTENT_H
    top = top or CONTENT_Y
    return top + max(0.0, avail - needed) * bias


def line_height(pt: float) -> float:
    return pt * LINE_SPACING / 72


def nice_axis(lo: float, hi: float, include_zero: bool) -> tuple[float, float, float]:
    """Круглые границы и шаг оси значений: (min, max, major unit) с запасом сверху под подписи."""
    if include_zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    span = (hi - lo) or abs(hi) or 1.0
    raw = span / 5
    mag = 10 ** math.floor(math.log10(raw))
    step = next((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), 10 * mag)
    mn = math.floor(lo / step + 1e-9) * step
    mx = math.ceil(hi / step - 1e-9) * step
    if (mx - hi) < step * 0.2:  # подписи «снаружи» не должны упираться в край
        mx += step
    return round(mn, 10), round(mx, 10), round(step, 10)


def para_gap(pt: float) -> float:
    return pt * 0.5 / 72


def block_height(
    paras: list[str], pt: float, width: float, *, bold: bool = False, bullet: bool = False
) -> float:
    """Высота блока абзацев (каждый — отдельный абзац с интервалом)."""
    w = width - (BULLET_INDENT if bullet else 0)
    total = 0.0
    for text in paras:
        total += len(wrap_lines(text, pt, w, bold)) * line_height(pt)
    return total + para_gap(pt) * max(0, len(paras) - 1)


def fit_size(
    paras: list[str],
    width: float,
    height: float,
    max_pt: int = BODY_MAX,
    min_pt: int = BODY_FLOOR,
    *,
    bold: bool = False,
    bullet: bool = False,
) -> int | None:
    """Наибольший кегль из [min_pt, max_pt], при котором блок помещается; None — не помещается."""
    inner = width - (BULLET_INDENT if bullet else 0)
    for pt in range(max_pt, min_pt - 1, -1):
        if (
            longest_word_width(paras, pt, bold) <= inner
            and block_height(paras, pt, width, bold=bold, bullet=bullet) <= height
        ):
            return pt
    return None


def title_fit(
    title: str, width: float, height: float = TITLE_BOX[3], max_pt: int = TITLE_MAX
) -> tuple[int, str]:
    """(кегль, текст) заголовка: 40 -> 32 pt; если и 32 pt не помещается в 2 строки,
    текст обрезается по слову. Заголовки не опускаются ниже TITLE_MIN."""
    for pt in range(max_pt, TITLE_MIN - 1, -2):
        if (
            longest_word_width([title], pt, True) <= width
            and block_height([title], pt, width, bold=True) <= height
        ):
            return pt, title
    words = title.split()
    while len(words) > 1:
        words.pop()
        candidate = " ".join(words).rstrip(" ,;:—-") + "…"
        if (
            longest_word_width([candidate], TITLE_MIN, True) <= width
            and block_height([candidate], TITLE_MIN, width, bold=True) <= height
        ):
            return TITLE_MIN, candidate
    for pt in range(TITLE_MIN - 2, 23, -2):  # очень длинное слово: ниже 32 pt только ради него
        if longest_word_width([title], pt, True) <= width:
            return pt, title
    return TITLE_MIN, title[:40] + "…"


# --- Геометрия отдельных layout'ов (используется и планировщиком, и рендером) -------------------


def card_grid(n: int) -> list[tuple[float, float, float, float]]:
    """Прямоугольники карточек (x, y, w, h) для n = 1..4."""
    if n <= 3:
        w = (CONTENT_W - GAP * (n - 1)) / n
        return [(MX + i * (w + GAP), CONTENT_Y, w, CONTENT_H) for i in range(n)]
    w = (CONTENT_W - GAP) / 2
    h = (CONTENT_H - GAP) / 2
    return [(MX + (i % 2) * (w + GAP), CONTENT_Y + (i // 2) * (h + GAP), w, h) for i in range(n)]


CARD_PAD = 0.28
CARD_BAR = 0.09


def card_inner(box: tuple[float, float, float, float]) -> tuple[float, float]:
    """(ширина, высота) области текста внутри карточки."""
    return box[2] - 2 * CARD_PAD, box[3] - CARD_BAR - 2 * CARD_PAD


def step_columns(n: int) -> list[tuple[float, float]]:
    w = (CONTENT_W - GAP * (n - 1)) / n
    return [(MX + i * (w + GAP), w) for i in range(n)]


def column_text_boxes() -> tuple[
    tuple[float, float, float, float], tuple[float, float, float, float]
]:
    w = (CONTENT_W - GAP) / 2
    return (MX, CONTENT_Y, w, CONTENT_H), (MX + w + GAP, CONTENT_Y, w, CONTENT_H)


def bullets_box(has_image: bool) -> tuple[float, float, float, float]:
    """Область пунктов; слева от неё рисуется акцентная полоса."""
    x = MX + 0.3
    right = SIDE_IMG_BOX[0] - GAP if has_image else SLIDE_W - MX
    return (x, CONTENT_Y, right - x, CONTENT_H)


def image_slide_text_box() -> tuple[float, float, float, float]:
    x = WIDE_IMG_BOX[0] + WIDE_IMG_BOX[2] + GAP + 0.1
    return (x, CONTENT_Y, SLIDE_W - MX - x, CONTENT_H)


def table_geometry(table: Table, pt: int) -> tuple[list[float], list[float]]:
    """(ширины колонок, высоты строк) в дюймах для таблицы при данном кегле. Первая строка —
    шапка (если есть). Колонка никогда не уже самого длинного слова (слова не рвутся)."""
    ncols = max(len(table.headers), max((len(r) for r in table.rows), default=0))
    pad = 0.12
    all_rows = ([table.headers] if table.headers else []) + table.rows
    weights, min_w = [], []
    for c in range(ncols):
        cells = [
            (row[c], bool(table.headers) and i == 0)
            for i, row in enumerate(all_rows)
            if c < len(row)
        ]
        weights.append(max(6, min(36, max((len(t) for t, _ in cells), default=6))))
        widest = max((text_width(w, pt, bold) for t, bold in cells for w in t.split()), default=0.5)
        min_w.append(widest + 2 * pad + 0.08)
    total = sum(weights)
    widths = [max(min_w[c], CONTENT_W * weights[c] / total) for c in range(ncols)]
    excess = sum(widths) - CONTENT_W
    if excess > 0:  # сжимаем только то, что шире минимума
        flex = [w - m for w, m in zip(widths, min_w, strict=True)]
        if sum(flex) >= excess:
            widths = [w - excess * f / sum(flex) for w, f in zip(widths, flex, strict=True)]
        else:  # даже минимумы не помещаются — пропорциональное сжатие (слова могут переноситься)
            scale = CONTENT_W / sum(widths)
            widths = [w * scale for w in widths]
    heights = []
    for i, row in enumerate(all_rows):
        bold = bool(table.headers) and i == 0
        lines = max(
            (len(wrap_lines(cell, pt, widths[c] - 2 * pad, bold)) for c, cell in enumerate(row)),
            default=1,
        )
        heights.append(max(0.5, lines * line_height(pt) + 2 * pad))
    return widths, heights


# --- Карточки: две раскладки (сетка и нумерованный список) -------------------------------------------

CARD_MIN_H, CARD_MAX_H = 1.6, 3.2
NUM_BADGE = 0.7  # диаметр номера в нумерованных раскладках
Box = tuple[float, float, float, float]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class CardsGeom:
    ok: bool
    variant: str  # grid | numbered
    size: int  # кегль текста
    title_pt: int
    boxes: list[Box]  # карточки / строки
    texts: list[tuple[Box | None, Box | None]]  # (заголовок, текст) каждой карточки


def _card_need(title: str, text: str, title_pt: int, size: int, iw: float) -> float:
    t = block_height([title], title_pt, iw, bold=True) + 0.12 if title else 0.0
    return t + (block_height([text], size, iw) if text else 0.0)


def cards_geometry(cards: list[tuple[str, str]], variant: str) -> CardsGeom:
    n = len(cards)
    titles = [t for t, _ in cards if t]
    texts = [x for _, x in cards if x]
    if variant == "numbered":
        return _numbered_geometry(cards, titles, texts)
    base = card_grid(n)
    iw = card_inner(base[0])[0]
    rows = 1 if n <= 3 else 2
    max_h = min(CARD_MAX_H, (CONTENT_H - GAP * (rows - 1)) / rows)
    inner_max = max_h - CARD_BAR - 2 * CARD_PAD
    title_pt = next(
        (pt for pt in (20, 18, 16, 14) if longest_word_width(titles, pt, True) <= iw), 0
    )
    size = 0
    for pt in range(24 if n <= 3 else 22, BODY_FLOOR - 1, -1):
        if longest_word_width(texts, pt) > iw:
            continue
        if all(_card_need(t, x, title_pt or 14, pt, iw) <= inner_max for t, x in cards):
            size = pt
            break
    ok = bool(title_pt and size)
    title_pt, size = title_pt or 14, size or BODY_FLOOR
    need = max(_card_need(t, x, title_pt, size, iw) for t, x in cards)
    h = _clamp(need + CARD_BAR + 2 * CARD_PAD, CARD_MIN_H, max_h)
    top = CONTENT_Y + (CONTENT_H - (rows * h + GAP * (rows - 1))) / 2
    w = base[0][2]
    boxes: list[Box] = []
    for i in range(n):
        col, row = (i, 0) if n <= 3 else (i % 2, i // 2)
        boxes.append((MX + col * (w + GAP), top + row * (h + GAP), w, h))
    tx = []
    for (t, x), b in zip(cards, boxes, strict=True):
        ix, iy = b[0] + CARD_PAD, b[1] + CARD_BAR + CARD_PAD
        th = block_height([t], title_pt, iw, bold=True) if t else 0.0
        used = th + (0.12 if t else 0.0)
        tx.append(
            (
                (ix, iy, iw, th) if t else None,
                (ix, iy + used, iw, b[3] - used - CARD_BAR - 2 * CARD_PAD) if x else None,
            )
        )
    return CardsGeom(ok, "grid", size, title_pt, boxes, tx)


def _numbered_geometry(cards, titles, texts) -> CardsGeom:
    n = len(cards)
    x0 = MX + 0.3
    title_x = x0 + NUM_BADGE + 0.3
    title_w = 3.4 if titles else 0.0
    text_x = title_x + (title_w + 0.3 if titles else 0.0)
    text_w = SLIDE_W - MX - 0.3 - text_x
    pad, gap = 0.16, 0.14
    title_pt = next(
        (
            pt
            for pt in (20, 18, 16, 14)
            if not titles or longest_word_width(titles, pt, True) <= title_w
        ),
        0,
    )
    best = None
    for pt in range(22, BODY_FLOOR - 1, -1):
        if texts and longest_word_width(texts, pt) > text_w:
            continue
        heights = []
        for t, x in cards:
            th = block_height([t], title_pt or 14, title_w, bold=True) if t else 0.0
            xh = block_height([x], pt, text_w) if x else 0.0
            heights.append(max(NUM_BADGE, th, xh) + 2 * pad)
        if sum(heights) + gap * (n - 1) <= CONTENT_H:
            best = (pt, heights)
            break
    ok = bool(title_pt and best)
    if best is None:
        best = (BODY_FLOOR, [NUM_BADGE + 2 * pad] * n)
    size, heights = best
    top = CONTENT_Y + (CONTENT_H - sum(heights) - gap * (n - 1)) / 2
    boxes, tx, y = [], [], top
    for (t, x), h in zip(cards, heights, strict=True):
        boxes.append((MX, y, CONTENT_W, h))
        ch = h - 2 * pad
        tx.append(
            (
                (title_x, y + pad, title_w, ch) if t else None,
                (text_x, y + pad, text_w, ch) if x else None,
            )
        )
        y += h + gap
    return CardsGeom(ok, "numbered", size, title_pt or 14, boxes, tx)


def pick_cards_variant(cards: list[tuple[str, str]], preferred: str) -> CardsGeom | None:
    """Предпочтительная раскладка, если помещается; иначе другая; None — не помещается ни одна."""
    order = [preferred, "numbered" if preferred == "grid" else "grid"]
    for variant in order:
        geom = cards_geometry(cards, variant)
        if geom.ok:
            return geom
    return None


# --- Таймлайн: горизонтальный (h), в две строки (h2) или вертикальный (v) -------------------------------


@dataclass
class TimelineGeom:
    ok: bool
    variant: str
    title_pt: int
    body_pt: int
    dots: list[Box]
    titles: list[Box]
    texts: list[Box]
    lines: list[Box]  # соединительные линии


def timeline_geometry(steps: list[tuple[str, str]], variant: str) -> TimelineGeom:
    n = len(steps)
    titles_all = [t for t, _ in steps]
    texts_all = [x for _, x in steps]
    if variant == "v":
        return _timeline_vertical(steps, titles_all, texts_all)
    per = n if variant == "h" else math.ceil(n / 2)
    groups = [list(range(i, min(i + per, n))) for i in range(0, n, per)]
    cols = step_columns(per)
    w = cols[0][1]
    d, dgap = (0.9, 0.4) if variant == "h" else (0.6, 0.25)
    tmax, bmax = (24, 22) if variant == "h" else (22, 20)
    max_th, max_bh = (1.1, 2.6) if variant == "h" else (0.85, 0.95)
    tsize = next(
        (
            pt
            for pt in range(tmax, 15, -1)
            if longest_word_width(titles_all, pt, True) <= w
            and max(block_height([t], pt, w, bold=True) for t in titles_all) <= max_th
        ),
        0,
    )
    bsize = next(
        (
            pt
            for pt in range(bmax, BODY_FLOOR - 1, -1)
            if longest_word_width(texts_all, pt) <= w
            and max(block_height([x], pt, w) for x in texts_all) <= max_bh
        ),
        0,
    )
    ok = bool(tsize and bsize)
    tsize, bsize = tsize or 16, bsize or BODY_FLOOR
    th = max(block_height([t], tsize, w, bold=True) for t in titles_all)
    bh = max(block_height([x], bsize, w) for x in texts_all)
    row_h = d + dgap + th + 0.2 + bh
    row_gap = 0.4
    total = len(groups) * row_h + row_gap * (len(groups) - 1)
    ok = ok and total <= CONTENT_H + 0.01
    top = CONTENT_Y + max(0.0, CONTENT_H - total) * 0.5
    dots, tbs, xbs, lines = [], [], [], []
    for r, group in enumerate(groups):
        y = top + r * (row_h + row_gap)
        lines.append((MX, y + d / 2 - 0.03, CONTENT_W, 0.06))
        for c, i in enumerate(group):
            x = cols[c][0]
            dots.append((x, y, d, d))
            tbs.append((x, y + d + dgap, w, th))
            xbs.append((x, y + d + dgap + th + 0.2, w, bh))
    return TimelineGeom(ok, variant, tsize, bsize, dots, tbs, xbs, lines)


def _timeline_vertical(steps, titles_all, texts_all) -> TimelineGeom:
    n = len(steps)
    d = 0.6
    x0 = MX + 0.1
    title_x = x0 + d + 0.35
    title_w = 3.3
    text_x = title_x + title_w + 0.3
    text_w = SLIDE_W - MX - text_x
    gap = 0.12
    tsize = next(
        (pt for pt in range(22, 15, -1) if longest_word_width(titles_all, pt, True) <= title_w), 0
    )
    for bsize in range(20, BODY_FLOOR - 1, -1):
        if texts_all and longest_word_width(texts_all, bsize) > text_w:
            continue
        hs = [
            max(
                d,
                block_height([t], tsize or 16, title_w, bold=True),
                block_height([x], bsize, text_w) if x else 0.0,
            )
            + 0.1
            for t, x in steps
        ]
        if sum(hs) + gap * (n - 1) <= CONTENT_H:
            break
    else:
        bsize, hs = BODY_FLOOR, [d + 0.1] * n
    total = sum(hs) + gap * (n - 1)
    ok = bool(tsize) and total <= CONTENT_H + 0.01
    top = CONTENT_Y + max(0.0, CONTENT_H - total) * 0.4
    dots, tbs, xbs, y = [], [], [], top
    for h in hs:
        dots.append((x0, y + (h - d) / 2, d, d))
        tbs.append((title_x, y, title_w, h))
        xbs.append((text_x, y, text_w, h))
        y += h + gap
    line = (x0 + d / 2 - 0.03, top + hs[0] / 2, 0.06, max(0.1, total - hs[0] / 2 - hs[-1] / 2))
    return TimelineGeom(ok, "v", tsize or 16, bsize, dots, tbs, xbs, [line])


def pick_timeline(steps: list[tuple[str, str]]) -> TimelineGeom | None:
    n = len(steps)
    variants = (["h"] if n <= 5 else []) + (["h2"] if n >= 3 else []) + ["v"]
    for variant in variants:
        geom = timeline_geometry(steps, variant)
        if geom.ok:
            return geom
    return None


# --- Сравнение: две панели одинаковой высоты по содержимому ---------------------------------------------

CMP_HEAD_H = 0.85
VS_D = 0.7
VS_CLEAR = 0.45  # заголовок отстоит от круга VS не меньше


@dataclass
class ComparisonGeom:
    ok: bool
    size: int
    gap_pt: float
    title_pt: int
    top: float
    h: float
    panels: list[Box]
    heads: list[Box]  # текстовые рамки заголовков


def comparison_geometry(titles: list[str], bodies: list[list[str]]) -> ComparisonGeom:
    lbox, rbox = column_text_boxes()
    cx = SLIDE_W / 2
    left_end = cx - VS_D / 2 - VS_CLEAR
    right_start = cx + VS_D / 2 + VS_CLEAR
    heads = [
        (lbox[0] + 0.25, 0.0, left_end - lbox[0] - 0.25, CMP_HEAD_H),
        (right_start, 0.0, rbox[0] + rbox[2] - 0.25 - right_start, CMP_HEAD_H),
    ]
    tp = [
        fit_size([t], h[2], CMP_HEAD_H - 0.2, 24, 18, bold=True)
        for t, h in zip(titles, heads, strict=True)
    ]
    inner_w = lbox[2] - 2 * CARD_PAD
    avail = CONTENT_H - CMP_HEAD_H - 0.7
    sizes = [fit_size(b, inner_w, avail, 24, BODY_FLOOR, bullet=True) for b in bodies]
    ok = all(tp) and all(sizes)
    size = min((x for x in sizes if x), default=BODY_FLOOR)
    gap_pt = size * 0.9
    need = max(
        block_height(b, size, inner_w, bullet=True)
        + (gap_pt - size * 0.5) / 72 * max(0, len(b) - 1)
        for b in bodies
    )
    h = _clamp(CMP_HEAD_H + 0.3 + need + 0.4, 2.6, CONTENT_H)
    ok = ok and h <= CONTENT_H
    top = CONTENT_Y + (CONTENT_H - h) / 2
    panels = [(lbox[0], top, lbox[2], h), (rbox[0], top, rbox[2], h)]
    heads = [(x, top, w, hh) for x, _, w, hh in heads]
    return ComparisonGeom(
        ok, size, gap_pt, min((x for x in tp if x), default=18), top, h, panels, heads
    )


# --- Планировщик ---------------------------------------------------------------------------


def _chunks(items: list, size: int) -> list[list]:
    """Делит на примерно равные части размером не больше size."""
    if not items:
        return [[]]
    parts = math.ceil(len(items) / size)
    per = math.ceil(len(items) / parts)
    return [items[i : i + per] for i in range(0, len(items), per)]


def _halves(items: list) -> tuple[list, list]:
    k = math.ceil(len(items) / 2)
    return items[:k], items[k:]


def _suffix(slides: list[Slide]) -> list[Slide]:
    n = len(slides)
    out = []
    for i, s in enumerate(slides, 1):
        out.append(replace(s, part=i, parts=n, title=f"{s.title} ({i}/{n})" if n > 1 else s.title))
    return out


def _clip(text: str, limit: int) -> str:
    return (
        text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"
    )


class Planner:
    def __init__(self, deck: Deck) -> None:
        self.deck = deck

    def note(self, event: str, slide: Slide | None = None, detail: str = "") -> None:
        self.deck.stats[event] += 1
        where = f" (слайд {slide.uid + 1})" if slide is not None else ""
        logger.warning("deck layout: %s%s%s", event, where, f" — {detail}" if detail else "")

    # каждое правило возвращает список слайдов (1 — влезло, 2+ — разбито)

    def plan(self) -> list[Slide]:
        out: list[Slide] = []
        for slide in self.deck.slides:
            parts = self._plan_slide(slide)
            if len(parts) > 1:
                self.deck.stats["slides_split"] += 1
                logger.warning(
                    "deck layout: slide_split (слайд %s -> %s частей)", slide.uid + 1, len(parts)
                )
            out.extend(parts)
        if len(out) > MAX_SLIDES:
            self.note("slides_over_cap_after_split", detail=f"{len(out)} -> {MAX_SLIDES}")
            out = out[:MAX_SLIDES]
        out = self._vary(out)
        self._finish(out)
        return out

    # --- разнообразие: один layout не идёт дважды подряд ---
    def _alternative(self, s: Slide) -> Slide | None:
        """Другой layout для того же содержания — только если смысл сохраняется."""
        if s.layout == "bullets" and not s.image_prompt and len(s.bullets) >= 2:
            pairs = [label_split(b) for b in s.bullets]
            if all(pairs):
                labels = list(dict.fromkeys(t.casefold() for t, _ in pairs))
                if len(labels) == len(pairs) and len(pairs) <= 4:  # «заголовок: пояснение»
                    return replace(s, layout="cards", cards=list(pairs), bullets=[])
                if len(labels) == 2:  # честное деление на две группы -> сравнение
                    names = list(dict.fromkeys(t for t, _ in pairs))
                    left = [x for t, x in pairs if t.casefold() == labels[0]]
                    right = [x for t, x in pairs if t.casefold() == labels[1]]
                    return replace(
                        s,
                        layout="comparison",
                        cards=[(names[0], ""), (names[1], "")],
                        left=left,
                        right=right,
                        bullets=[],
                    )
        if s.layout == "cards" and all(len(f"{t} {x}".split()) <= 22 for t, x in s.cards):
            items = [f"{t}: {x}" if t and x else (t or x) for t, x in s.cards]
            return replace(s, layout="bullets", cards=[], bullets=items)
        return None

    def _vary(self, slides: list[Slide]) -> list[Slide]:
        for i in range(1, len(slides)):
            prev, cur = slides[i - 1], slides[i]
            if (
                cur.layout != prev.layout
                or cur.uid == prev.uid
                or cur.layout in ("title", "section")
            ):
                continue
            alt = self._alternative(cur)
            parts = self._plan_slide(alt) if alt else []
            if len(parts) == 1 and parts[0].layout != prev.layout:
                slides[i] = replace(parts[0], part=cur.part, parts=cur.parts)
                self.note("layout_repeat_fixed", cur, f"{cur.layout} -> {parts[0].layout}")
            else:
                self.note("layout_repeat_kept", cur, cur.layout)
        return slides

    def _finish(self, slides: list[Slide]) -> None:
        """Номера разделов и чередование раскладок карточек (сетка / нумерованный список)."""
        sections = cards_seen = 0
        for s in slides:
            if s.layout == "section":
                sections += 1
                s.number = sections
            elif s.layout == "cards":
                preferred = "grid" if cards_seen % 2 == 0 else "numbered"
                cards_seen += 1
                geom = pick_cards_variant(s.cards, preferred)
                s.variant = geom.variant if geom else preferred

    def _plan_slide(self, s: Slide) -> list[Slide]:
        handler = getattr(self, f"_plan_{s.layout}", None)
        return handler(s) if handler else [s]

    # --- списки ---
    def _fit_or_halve(self, s: Slide, items: list[str], width: float, key: str) -> list[Slide]:
        """Слайд как есть, если список помещается при кегле >= 14 pt, иначе две половины."""
        if (
            len(items) > 1
            and fit_size(items, width, CONTENT_H, BODY_MAX, BODY_FLOOR, bullet=True) is None
        ):
            self.note("overflow_split", s, key)
            first, second = _halves(items)
            return [replace(s, **{key: first}), replace(s, **{key: second}, image_prompt="")]
        return [replace(s, **{key: items})]

    def _bullets_parts(self, s: Slide, items: list[str], width: float, key: str) -> list[Slide]:
        """Пункты по слайдам: не более DECK_MAX_BULLETS на слайд и без переполнения."""
        chunks = [items]
        if len(items) > DECK_MAX_BULLETS:
            self.note("bullets_split", s, f"{len(items)} пунктов")
            chunks = _chunks(items, DECK_MAX_BULLETS)
        parts: list[Slide] = []
        for chunk in chunks:
            parts += self._fit_or_halve(s, chunk, width, key)
        return _suffix(parts) if len(parts) > 1 else parts

    def _plan_bullets(self, s: Slide) -> list[Slide]:
        box = bullets_box(bool(s.image_prompt))
        parts = self._bullets_parts(s, s.bullets, box[2], "bullets")
        for extra in parts[1:]:
            extra.image_prompt = ""
        if s.layout == "bullets" and len(parts) == 1 and not s.image_prompt:
            alt = self._sparse_alternative(s, box)
            if alt is not None:
                return alt
        return parts

    SPARSE_BULLETS = 0.45  # доля области контента, ниже которой список выглядит пустым

    def _sparse_alternative(self, s: Slide, box) -> list[Slide] | None:
        """Мало текста на слайде: «заголовок: пояснение» -> карточки; список из >= 4 пунктов —
        две колонки (порядок сохраняется). Иначе оставляем как есть."""
        items = s.bullets
        pt = fit_size(items, box[2], box[3], 28 if len(items) <= 4 else 24, BODY_FLOOR, bullet=True)
        pt = pt or BODY_FLOOR
        covered = block_height(items, pt, box[2], bullet=True) / CONTENT_H
        if covered >= self.SPARSE_BULLETS:
            return None
        pairs = [label_split(b) for b in items]
        alt = None
        if (
            all(pairs)
            and 2 <= len(items) <= 4
            and len({t.casefold() for t, _ in pairs}) == len(items)
        ):
            alt = replace(s, layout="cards", cards=list(pairs), bullets=[])
        elif len(items) >= 4:
            left, right = _halves(items)
            alt = replace(s, layout="two_column", left=left, right=right, bullets=[])
        parts = self._plan_slide(alt) if alt else []
        if len(parts) == 1:
            self.note("sparse_layout_changed", s, f"{covered:.0%} -> {alt.layout}")
            return parts
        return None

    _plan_closing = _plan_bullets

    def _plan_image(self, s: Slide) -> list[Slide]:
        box = image_slide_text_box()
        parts = self._bullets_parts(s, s.bullets, box[2], "bullets")
        for extra in parts[1:]:
            extra.layout, extra.image_prompt = "bullets", ""
        return parts

    def _plan_two_column(self, s: Slide) -> list[Slide]:
        lbox, _ = column_text_boxes()
        inner_w = lbox[2] - 2 * CARD_PAD
        inner_h = lbox[3] - 2 * CARD_PAD
        fits = all(
            fit_size(col, inner_w, inner_h, BODY_MAX, BODY_FLOOR, bullet=True) is not None
            for col in (s.left, s.right)
            if col
        )
        if not fits and max(len(s.left), len(s.right)) > 1:
            self.note("overflow_split", s, "two_column")
            la, lb = _halves(s.left)
            ra, rb = _halves(s.right)
            return _suffix(
                [replace(s, left=la, right=ra), replace(s, left=lb, right=rb, image_prompt="")]
            )
        return [s]

    # --- карточки / сравнение ---
    def _plan_cards(self, s: Slide) -> list[Slide]:
        out: list[Slide] = []
        for chunk in _chunks(s.cards, 4):
            if len(chunk) > 1 and pick_cards_variant(chunk, "grid") is None:
                self.note("overflow_split", s, "cards")
                a, b = _halves(chunk)
                out += [replace(s, cards=a), replace(s, cards=b)]
            else:
                out.append(replace(s, cards=chunk))
        if len(out) > 1:
            self.note("cards_split", s, f"{len(s.cards)} карточек")
        return _suffix(out) if len(out) > 1 else out

    def _plan_comparison(self, s: Slide) -> list[Slide]:
        s = replace(s, cards=s.cards[:2])
        titles = [t for t, _ in s.cards]
        if (
            not comparison_geometry(titles, [s.left or [""], s.right or [""]]).ok
            and max(len(s.left), len(s.right)) > 1
        ):
            self.note("overflow_split", s, "comparison")
            la, lb = _halves(s.left)
            ra, rb = _halves(s.right)
            return _suffix([replace(s, left=la, right=ra), replace(s, left=lb, right=rb)])
        return [s]

    # --- числа ---
    def _plan_stat(self, s: Slide) -> list[Slide]:
        parts = _chunks(s.stats, 4)
        out = [replace(s, stats=p) for p in parts]
        for extra in out[1:]:
            extra.bullets = []
        if len(out) > 1:
            self.note("stats_split", s, f"{len(s.stats)} показателей")
        return _suffix(out) if len(out) > 1 else out

    # --- таймлайн ---
    def _plan_timeline(self, s: Slide) -> list[Slide]:
        parts = _chunks(s.steps, 6)
        if len(parts) > 1:
            self.note("steps_split", s, f"{len(s.steps)} шагов")
        out = []
        for chunk in parts:
            geom = pick_timeline(chunk)
            if geom is None and len(chunk) > 2:  # не помещается даже вертикально — делим
                self.note("overflow_split", s, "timeline")
                a, b = _halves(chunk)
                out += [replace(s, steps=a, variant="v"), replace(s, steps=b, variant="v")]
                continue
            out.append(replace(s, steps=chunk, variant=geom.variant if geom else "v"))
        return _suffix(out) if len(out) > 1 else out

    # --- таблица ---
    def _plan_table(self, s: Slide) -> list[Slide]:
        table = s.table
        assert table is not None
        rows = table.rows
        if not rows:
            return [s]
        out_rows: list[list[list[str]]] = []
        current: list[list[str]] = []
        for row in rows:
            candidate = current + [row]
            _, heights = table_geometry(Table(table.headers, candidate), BODY_FLOOR + 2)
            if current and (len(candidate) > DECK_TABLE_ROWS_PER_SLIDE or sum(heights) > CONTENT_H):
                out_rows.append(current)
                current = [row]
            else:
                current = candidate
        out_rows.append(current)
        if len(out_rows) > 1:
            self.note("table_split", s, f"{len(rows)} строк -> {len(out_rows)} слайдов")
        out = [replace(s, table=Table(table.headers, chunk)) for chunk in out_rows]
        for extra in out[1:]:
            extra.bullets = []
        return _suffix(out) if len(out) > 1 else out

    # --- цитата ---
    def _plan_quote(self, s: Slide) -> list[Slide]:
        assert s.quote is not None
        text, author = s.quote
        width, height = CONTENT_W - 1.6, CONTENT_H - 1.4
        if fit_size([text], width, height, 32, BODY_FLOOR) is None:
            self.note("quote_truncated", s)
            while len(text) > 40 and fit_size([text], width, height, 32, BODY_FLOOR) is None:
                text = _clip(text, int(len(text) * 0.8))
            return [replace(s, quote=(text, author))]
        return [s]

    def _plan_chart(self, s: Slide) -> list[Slide]:
        """Диаграмма + 2-3 вывода справа. Нет выводов от модели — считаем кодом по числам."""
        bullets = s.bullets
        if len(bullets) > 3:
            self.note("chart_bullets_trimmed", s, f"{len(bullets)} -> 3")
            bullets = bullets[:3]
        if len(bullets) < 2 and s.chart is not None:
            computed = chart_takeaways(s.chart, s.title)
            if computed:
                self.note("chart_takeaways_computed", s, f"{len(computed)}")
                bullets = computed
        return [replace(s, bullets=bullets)]


def plan_slides(deck: Deck) -> list[Slide]:
    """Раскладывает слайды колоды: разбивает переполненные, ограничивает числа элементов.
    Счётчики событий пишутся в deck.stats (+ warning в лог)."""
    return Planner(deck).plan()
