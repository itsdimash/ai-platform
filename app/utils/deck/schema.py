"""Схема инструмента generate_presentation и нормализация ответа модели.

Схема компактная и без unions/default/$ref: одинаково принимается Claude, Gemini и GPT
(проверено живыми вызовами). Каждый токен схемы оплачивается в КАЖДОМ запросе с
инструментами, поэтому описания короткие; правила содержания — в системном промпте.

normalize() терпимо разбирает то, что вернула модель, и считает/логирует каждое исправление
(logger.warning + Counter в Deck.stats): неизвестный layout, обрезанный заголовок, лишние
пункты, битые числа и т.д.
"""

import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.limits import (
    DECK_CHART_MAX_CATEGORIES,
    DECK_CHART_MAX_SERIES,
    DECK_MAX_BULLETS,
    DECK_TABLE_MAX_COLS,
    MAX_SLIDES,
)
from app.utils.deck.themes import DEFAULT_THEME, PALETTES
from app.utils.tool_schema import SUMMARY_PROP

logger = logging.getLogger(__name__)

LAYOUTS = (
    "title",
    "section",
    "bullets",
    "cards",
    "stat",
    "two_column",
    "comparison",
    "timeline",
    "image",
    "table",
    "chart",
    "quote",
    "closing",
)
IMAGE_LAYOUTS = {"title", "bullets", "image"}  # layout'ы, которые умеют показывать картинку
MAX_TITLE_CHARS = 80
MAX_SUBTITLE_CHARS = 160
MAX_FOOTNOTE_CHARS = 120

_STR = {"type": "string"}


def _arr(item: dict) -> dict:
    return {"type": "array", "items": item}


def _obj(**props: dict) -> dict:
    return {"type": "object", "properties": props}


PRESENTATION_TOOL = {
    "name": "generate_presentation",
    "description": (
        "Create a themed 16:9 .pptx deck. Default 8-12 slides: context, 3-5 meaning blocks, "
        "numbers/examples, conclusions. One idea per slide, max 5 bullets of ~12 words, titles "
        "state a conclusion. Never leave blanks: no '____', '[...]', TBD, XXX, lorem, 'fill in'; "
        "if you lack a number, state it qualitatively or as an explicit assumption (\u00abориентир "
        "~20%\u00bb). Never invent statistics. Currency: tenge (\u20b8) unless the user names "
        "another. Alternate layouts: never the same layout twice in a row. stat = 2-4 numbers only "
        "(value <=8 chars, e.g. 24%, +18%, 7 дней; never words). comparison: cards[0..1].title are "
        "headers, left/right = one point per array item, never a comma-joined sentence. section "
        "only in decks of 12+ slides. If you create illustrative numbers, make them arithmetically "
        "consistent across the whole deck: totals equal sums of parts, percentages and growth are "
        "computed from the figures shown, the same number never has different values on different "
        "slides. Recompute before output. One footnote per deck, only on the first data slide, "
        "short (\u00abУсловные данные\u00bb); never put instructions for the user (e.g. replace with "
        "actual data) on slides, put them in summary. "
        "Speaker notes (2-4 sentences) on every slide except the title. User's language."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": _STR,
            "subtitle": _STR,
            "theme": {
                "type": "string",
                "enum": list(PALETTES),
                "description": "Pick by topic; graphite if unsure",
            },
            "image_prompt": {
                "type": "string",
                "description": (
                    "English hero image for the title slide. Visual topics only (animals, places, "
                    "products, food, people, travel); omit for finance/ERP/IT/reporting. No text or "
                    "lettering, no logos or real brands, no recognizable people"
                ),
            },
            "summary": SUMMARY_PROP,
            "slides": {
                "type": "array",
                "description": "Slides. The first may be layout=title, otherwise it is built from title/subtitle",
                "items": {
                    "type": "object",
                    "properties": {
                        "layout": {"type": "string", "enum": list(LAYOUTS)},
                        "title": _STR,
                        "subtitle": _STR,
                        "bullets": {**_arr(_STR), "description": "max 5, ~12 words each"},
                        "cards": _arr(_obj(title=_STR, text=_STR)),
                        "stats": {
                            **_arr(_obj(value=_STR, label=_STR)),
                            "description": "2-4; value is a number <=8 chars",
                        },
                        "left": {**_arr(_STR), "description": "one point per item"},
                        "right": {**_arr(_STR), "description": "one point per item"},
                        "steps": _arr(_obj(title=_STR, text=_STR)),
                        # плоско, без вложенного объекта: модель реже ломает JSON (см. normalize)
                        "table_headers": _arr(_STR),
                        "table_rows": _arr(_arr(_STR)),
                        "chart": _obj(
                            type={"type": "string", "enum": ["bar", "line", "pie"]},
                            categories=_arr(_STR),
                            series=_arr(_obj(name=_STR, values=_arr({"type": "number"}))),
                            unit={"type": "string", "description": "e.g. млн ₸, %, шт."},
                        ),
                        "quote": _obj(text=_STR, author=_STR),
                        "notes": {
                            "type": "string",
                            "description": "speaker notes: 2-4 full sentences (250-450 chars)",
                        },
                        "footnote": {
                            "type": "string",
                            "description": "short note above the footer, e.g. «Условные данные»",
                        },
                        "image_prompt": {
                            "type": "string",
                            "description": (
                                "English, layout=image (or title); visual topics only, else omit. "
                                "No text/lettering, no logos or real brands, no recognizable people"
                            ),
                        },
                    },
                    "required": ["layout", "title"],
                },
            },
        },
        "required": ["title", "slides", "theme", "summary"],
    },
}


# --- Модель данных ---------------------------------------------------------------------


@dataclass
class Table:
    headers: list[str]
    rows: list[list[str]]


@dataclass
class Chart:
    type: str  # bar | line | pie
    categories: list[str]
    series: list[tuple[str, list[float]]]
    unit: str = ""


@dataclass
class Slide:
    layout: str
    title: str
    subtitle: str = ""
    bullets: list[str] = field(default_factory=list)
    cards: list[tuple[str, str]] = field(default_factory=list)
    stats: list[tuple[str, str]] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    right: list[str] = field(default_factory=list)
    steps: list[tuple[str, str]] = field(default_factory=list)
    table: Table | None = None
    chart: Chart | None = None
    quote: tuple[str, str] | None = None
    notes: str = ""
    image_prompt: str = ""
    footnote: str = ""  # короткая пометка над футером («Условные данные»)
    variant: str = (
        ""  # вариант раскладки (cards: grid|numbered; timeline: h|h2|v) — выбирает планировщик
    )
    number: int = 0  # номер раздела на section-слайде
    uid: int = 0  # индекс исходного слайда: картинки привязаны к нему, не к продолжениям
    part: int = 1  # номер части при разбиении слайда
    parts: int = 1


@dataclass
class Deck:
    title: str
    subtitle: str
    theme: str
    summary: str
    slides: list[Slide]
    hero_prompt: str = ""
    stats: Counter = field(default_factory=Counter)


# --- Нормализация ---------------------------------------------------------------------------


class _Norm:
    def __init__(self) -> None:
        self.stats: Counter = Counter()

    def note(self, event: str, slide: int | None = None, detail: str = "") -> None:
        self.stats[event] += 1
        where = f" (слайд {slide})" if slide is not None else ""
        logger.warning("deck normalization: %s%s%s", event, where, f" — {detail}" if detail else "")


_SLIDE_KEY = re.compile(
    r',\s*"(?:notes|footnote|image_prompt|subtitle|bullets|cards|stats|left|right|steps|table|chart|quote)":'
)


def _repair_object(line: str) -> Any:
    """Одна строка = один слайд. Модель иногда не закрывает вложенный объект (например, table):
    пробуем вставить недостающую скобку перед очередным ключом слайда."""
    for m in _SLIDE_KEY.finditer(line):
        for closer in ("}", "]", "]}", "}}"):
            try:
                return json.loads(line[: m.start()] + closer + line[m.start() :])
            except ValueError:
                continue
    return None


def _salvage_slides(text: str, norm: "_Norm") -> list:
    """Битый JSON-массив слайдов: разбираем построчно, чиним что можно, остальное теряем."""
    out = []
    for line in text.strip().strip("[]").split("\n"):
        line = line.strip().rstrip(",")
        if not line:
            continue
        try:
            out.append(json.loads(line))
            continue
        except ValueError:
            pass
        fixed = _repair_object(line)
        if fixed is None:
            norm.note("json_slide_lost", detail=line[:50])
        else:
            norm.note("json_slide_repaired", detail=line[:50])
            out.append(fixed)
    return out


def _jsonish(value: Any, norm: "_Norm", slide: int | None, what: str) -> Any:
    """Claude иногда отдаёт массив/объект JSON-СТРОКОЙ — разбираем её, а не теряем содержимое."""
    if isinstance(value, str) and value.lstrip()[:1] in ("[", "{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            if what == "slides" and value.lstrip().startswith("["):
                norm.note("json_string_parsed", slide, what)
                return _salvage_slides(value, norm)
            return value
        norm.note("json_string_parsed", slide, what)
        return parsed
    return value


_NBSP = "\u202f"  # узкий неразрывный пробел: число и единица не расходятся по строкам
_MINUS = "\u2212"
_LEADING_MINUS = re.compile(r"(?<![\w\u2212])-(?=\d)")
_UNIT_AFTER_NUM = re.compile(
    r"(?<=\d) (?=(?:млн|млрд|трлн|тыс|лет|дн|мес|нед|шт|кг|кв)\w*|[₸$€₽%])"
)
_CURRENCY_AFTER_UNIT = re.compile(r"\b(млн|млрд|трлн|тыс\.?) (?=[₸$€₽])")


def _s(value: Any) -> str:
    """Строка без лишних пробелов; число и единица («152 млн ₸») склеены неразрывным пробелом,
    чтобы валюта не оставалась одна на следующей строке."""
    if value is None:
        return ""
    text = re.sub(r"[ \t]+", " ", str(value)).strip()
    text = _LEADING_MINUS.sub(_MINUS, text)  # «-30%» -> «−30%»; диапазон «10-15» не трогаем
    text = _UNIT_AFTER_NUM.sub(_NBSP, text)
    return _CURRENCY_AFTER_UNIT.sub(lambda m: m.group(1) + _NBSP, text)


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [part for part in re.split(r"\n+|;\s*", value)]
    if not isinstance(value, list | tuple):
        return []
    return [t for t in (_s(v) for v in value) if t]


def _pairs(value: Any, keys: tuple[str, str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, dict):
            first, second = _s(item.get(keys[0])), _s(item.get(keys[1]))
        else:
            first, second = "", _s(item)
        if first or second:
            out.append((first, second))
    return out


def _number(value: Any, norm: _Norm, slide: int) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    text = re.sub(r"[\s %$€₸₽]", "", _s(value)).replace(",", ".")
    try:
        return float(text)
    except ValueError:
        norm.note("bad_number", slide, repr(value)[:40])
        return 0.0


def _cut_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return (cut or text[: limit - 1]) + "…"


# Плейсхолдеры в тексте слайдов недопустимы: пункт с таким текстом удаляется целиком.
_PLACEHOLDER = re.compile(
    r"_{3,}|\[\s*(?:\.{2,}|…)?\s*\]|\[[^\]]*(?:\.{3}|…|вставь|укаж|insert|your )[^\]]*\]"
    r"|\b(?:TBD|TBC|TODO|XXX+|lorem|ipsum)\b|(?<!\w)[хХ]{3,}(?!\w)|заполн(?:ить|ите)\b|\?{3,}",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?…])\s+|\n+")
_STAT_VALUE_MAX = 10  # символов; в описании инструмента модели обещано <= 8
_DATA_NOTE = re.compile(
    r"(?:условн|демонстрационн|примерн|иллюстративн|illustrative|sample|dummy|mock)\w*\s+"
    r"(?:данн|цифр|значени|data|figures)\w*",
    re.IGNORECASE,
)
_LABELLED = re.compile(r"^(?P<t>[^:–—]{2,40}?)\s*(?::|\s[—–]\s)\s*(?P<x>\S.{6,})$")
MIN_SECTION_DECK = 12
_STRUCTURED = {
    "bullets",
    "cards",
    "stats",
    "left",
    "right",
    "steps",
    "table",
    "chart",
    "quote",
    "table_headers",
    "table_rows",
}
_CLAUSE_WORDS = {
    "а",
    "но",
    "и",
    "что",
    "если",
    "чтобы",
    "когда",
    "потому",
    "поэтому",
    "хотя",
    "так",
    "тогда",
    "while",
    "but",
    "and",
    "if",
    "that",
    "because",
}


def has_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER.search(text or ""))


def _keep(items: list[str], norm: "_Norm", idx: int, what: str) -> list[str]:
    """Пункты без плейсхолдеров и инструкций; негодный пункт удаляется целиком."""
    return [t for t in (_clean_item(i, norm, idx, what) for i in items) if t is not None]


def _keep_pairs(
    items: list[tuple[str, str]], norm: "_Norm", idx: int, what: str
) -> list[tuple[str, str]]:
    out = []
    for a, b in items:
        ca = _clean_item(a, norm, idx, what) if a else ""
        cb = _clean_item(b, norm, idx, what) if b else ""
        if ca is None or cb is None or not (ca or cb):
            continue
        out.append((ca, cb))
    return out


def _clean_text(text: str, norm: "_Norm", idx: int | None, what: str) -> str:
    """Заметки/summary: предложения с плейсхолдером удаляются, остальное остаётся."""
    if not has_placeholder(text):
        return text
    parts = [p for p in _SENTENCE.split(text) if p]
    kept = [p for p in parts if not has_placeholder(p)]
    norm.note("placeholder_dropped", idx, f"{what}: {len(parts) - len(kept)} предл.")
    return " ".join(kept)


def split_points(text: str) -> list[str]:
    """Сравнение: одна мысль — один пункт. Режет по переводам строк, «;» и предложениям;
    список через запятую (>= 3 коротких фрагментов) — по запятым."""
    parts = [p.strip(" -•\t") for p in re.split(r"\n+|;\s*|(?<=[.!?])\s+", text) if p.strip(" -•")]
    if len(parts) == 1 and parts[0].count(",") >= 2:
        frags = [f.strip() for f in parts[0].split(",") if f.strip()]
        joined = any(f.split()[0].casefold() in _CLAUSE_WORDS for f in frags)
        if len(frags) >= 3 and not joined and all(len(f.split()) <= 4 for f in frags):
            parts = frags
    return [p[:1].upper() + p[1:] for p in parts] or [""]


def stat_value_ok(value: str) -> bool:
    """Показатель — число (с единицей), а не слово: есть цифра и не длиннее лимита."""
    return bool(re.search(r"\d", value)) and len(value) <= _STAT_VALUE_MAX


_INSTRUCTION = re.compile(
    r"замен\w*\s+(?:на\s+)?(?:факт|реальн|актуальн|настоящ)\w*"
    r"|(?:заменит|подставьт|уточнит|проверьт|обновит)\w*[^.;—–]{0,40}\b(?:перед|до)\s+"
    r"(?:показ|презентац|использован|выступлен)\w*"
    r"|перед\s+(?:показом|презентацией|использованием|выступлением)"
    r"|\b(?:replace|swap)\b[^.;]{0,30}\b(?:actual|real)\b|before\s+(?:presenting|use)",
    re.IGNORECASE,
)
_CLAUSES = re.compile(r"\s*[—–;]\s*|(?<=[.!?])\s+")


def strip_instruction(text: str) -> tuple[str, bool]:
    """Инструкции пользователю («заменить фактическими перед показом») не место на слайде:
    такие части фразы вырезаются. (текст без них, были ли они)."""
    parts = [p for p in _CLAUSES.split(text) if p]
    kept = [p for p in parts if not _INSTRUCTION.search(p)]
    if len(kept) == len(parts):
        return text, False
    return " — ".join(kept), True


def _clean_item(text: str, norm: "_Norm", idx: int | None, what: str) -> str | None:
    """None — пункт удалить (плейсхолдер или целиком инструкция)."""
    if has_placeholder(text):
        norm.note("placeholder_dropped", idx, f"{what}: {text[:40]!r}")
        return None
    clean, changed = strip_instruction(text)
    if changed:
        norm.note("instruction_stripped", idx, f"{what}: {text[:50]!r}")
        if len(clean.split()) < 4:
            return None
        return clean
    return text


def label_split(text: str) -> tuple[str, str] | None:
    """«Заголовок: пояснение» -> (заголовок, пояснение); заголовок не длиннее 6 слов."""
    m = _LABELLED.match(text.strip())
    if m and len(m["t"].split()) <= 6:
        return m["t"].strip(), m["x"].strip()
    return None


def _infer_layout(s: Slide) -> str:
    if s.chart:
        return "chart"
    if s.table:
        return "table"
    if s.stats:
        return "stat"
    if s.steps:
        return "timeline"
    if s.quote:
        return "quote"
    if len(s.cards) >= 2:
        return "cards"
    if s.left or s.right:
        return "two_column"
    return "bullets"


def _parse_table(raw: Any, norm: _Norm, idx: int) -> Table | None:
    if not isinstance(raw, dict):
        return None
    headers = [_s(h) for h in (raw.get("headers") or [])]
    rows = [[_s(c) for c in row] for row in (raw.get("rows") or []) if isinstance(row, list)]
    bad_cols = {i for i, h in enumerate(headers) if has_placeholder(h)}
    if bad_cols:
        norm.note("placeholder_dropped", idx, f"table: колонок {len(bad_cols)}")
        headers = [h for i, h in enumerate(headers) if i not in bad_cols]
        rows = [[c for i, c in enumerate(r) if i not in bad_cols] for r in rows]
    kept_rows = [r for r in rows if not any(has_placeholder(c) for c in r)]
    if len(kept_rows) != len(rows):
        norm.note("placeholder_dropped", idx, f"table: строк {len(rows) - len(kept_rows)}")
        if not kept_rows:
            return None
        rows = kept_rows
    ncols = max([len(headers)] + [len(r) for r in rows] or [0])
    if ncols == 0:
        return None
    if ncols > DECK_TABLE_MAX_COLS:
        norm.note("table_cols_truncated", idx, f"{ncols} -> {DECK_TABLE_MAX_COLS}")
        ncols = DECK_TABLE_MAX_COLS
    headers = (headers + [""] * ncols)[:ncols] if headers else []
    rows = [(r + [""] * ncols)[:ncols] for r in rows]
    return Table(headers, rows) if (headers or rows) else None


def _normalize_pie(values: list[float], unit: str, norm: "_Norm", idx: int) -> list[float]:
    """Доли в процентах должны давать ~100: иначе подписи диаграммы (она сама считает доли)
    разойдутся с цифрами в тексте. Приводим к сумме 100 и логируем."""
    total = sum(values)
    looks_like_percent = "%" in unit or (all(0 <= v <= 100 for v in values) and 90 <= total <= 110)
    if not looks_like_percent or total <= 0 or abs(total - 100) <= 1:
        return values
    norm.note("pie_sum_mismatch", idx, f"сумма {total:g} -> 100")
    scaled = [round(v * 100 / total, 1) for v in values]
    scaled[-1] = round(100 - sum(scaled[:-1]), 1)
    return scaled


def _parse_chart(raw: Any, norm: _Norm, idx: int) -> Chart | None:
    if not isinstance(raw, dict):
        return None
    ctype = _s(raw.get("type")).lower()
    if ctype not in ("bar", "line", "pie"):
        if ctype:
            norm.note("chart_type_fallback", idx, ctype)
        ctype = "bar"
    categories = _str_list(raw.get("categories"))
    series_raw = [s for s in (raw.get("series") or []) if isinstance(s, dict)]
    if not categories or not series_raw:
        return None
    bad_cat = {i for i, c in enumerate(categories) if has_placeholder(c)}
    if bad_cat:
        norm.note("placeholder_dropped", idx, f"chart: категорий {len(bad_cat)}")
        categories = [c for i, c in enumerate(categories) if i not in bad_cat]
        series_raw = [
            {**s_, "values": [v for i, v in enumerate(s_.get("values") or []) if i not in bad_cat]}
            for s_ in series_raw
        ]
        if not categories:
            return None
    if len(categories) > DECK_CHART_MAX_CATEGORIES:
        norm.note("chart_categories_truncated", idx, f"{len(categories)}")
        categories = categories[:DECK_CHART_MAX_CATEGORIES]
    if len(series_raw) > DECK_CHART_MAX_SERIES:
        norm.note("chart_series_truncated", idx, f"{len(series_raw)}")
        series_raw = series_raw[:DECK_CHART_MAX_SERIES]
    series: list[tuple[str, list[float]]] = []
    for n, item in enumerate(series_raw, 1):
        values = [_number(v, norm, idx) for v in (item.get("values") or [])]
        if len(values) != len(categories):
            norm.note("chart_values_length_fixed", idx, f"{len(values)} vs {len(categories)}")
            values = (values + [0.0] * len(categories))[: len(categories)]
        name = _s(item.get("name"))
        if has_placeholder(name):
            norm.note("placeholder_dropped", idx, f"chart: имя ряда {name[:30]!r}")
            name = ""
        series.append((name or f"Ряд {n}", values))
    unit = _s(raw.get("unit"))[:24]
    if has_placeholder(unit):
        unit = ""
    if ctype == "pie":
        series = series[:1]
        series[0] = (series[0][0], _normalize_pie(series[0][1], unit, norm, idx))
    return Chart(ctype, categories, series, unit)


def _parse_slide(raw: Any, idx: int, norm: _Norm) -> Slide | None:
    raw = _jsonish(raw, norm, idx, "slide")
    if not isinstance(raw, dict):
        norm.note("slide_not_object", idx)
        return None
    raw = {k: _jsonish(v, norm, idx, k) if k in _STRUCTURED else v for k, v in raw.items()}
    title = _s(raw.get("title"))
    notes = _clean_text(_s(raw.get("notes")), norm, idx, "notes")
    if has_placeholder(title):
        norm.note("placeholder_dropped", idx, f"title: {title[:40]!r}")
        title = ""
    title = _clean_item(title, norm, idx, "title") or "" if title else ""
    if len(title) > MAX_TITLE_CHARS:
        norm.note("title_truncated", idx, title[:40])
        notes = f"{notes}\nПолный заголовок: {title}".strip()
        title = _cut_words(title, MAX_TITLE_CHARS)
    subtitle = _s(raw.get("subtitle"))
    if has_placeholder(subtitle):
        norm.note("placeholder_dropped", idx, f"subtitle: {subtitle[:40]!r}")
        subtitle = ""
    clean, changed = strip_instruction(subtitle)
    if changed:
        norm.note("instruction_stripped", idx, f"subtitle: {subtitle[:50]!r}")
        subtitle = clean
    if len(subtitle) > MAX_SUBTITLE_CHARS:
        norm.note("subtitle_truncated", idx)
        subtitle = _cut_words(subtitle, MAX_SUBTITLE_CHARS)
    footnote = _s(raw.get("footnote"))
    if has_placeholder(footnote):
        norm.note("placeholder_dropped", idx, "footnote")
        footnote = ""
    clean, changed = strip_instruction(footnote)
    if changed:
        norm.note("instruction_stripped", idx, f"footnote: {footnote[:50]!r}")
        footnote = clean
    footnote = _cut_words(footnote, MAX_FOOTNOTE_CHARS)

    quote_raw = raw.get("quote")
    quote = None
    if isinstance(quote_raw, dict) and _s(quote_raw.get("text")):
        qtext, qauthor = _s(quote_raw.get("text")), _s(quote_raw.get("author"))
        if has_placeholder(qtext):
            norm.note("placeholder_dropped", idx, "quote")
        else:
            if has_placeholder(qauthor):
                norm.note("placeholder_dropped", idx, "quote author")
                qauthor = ""
            quote = (qtext, qauthor)

    slide = Slide(
        layout=_s(raw.get("layout")).lower(),
        title=title,
        subtitle=subtitle,
        bullets=_keep(_str_list(raw.get("bullets")), norm, idx, "bullet"),
        cards=_keep_pairs(_pairs(raw.get("cards"), ("title", "text")), norm, idx, "card"),
        stats=_keep_pairs(_pairs(raw.get("stats"), ("value", "label")), norm, idx, "stat"),
        left=_keep(_str_list(raw.get("left")), norm, idx, "left"),
        right=_keep(_str_list(raw.get("right")), norm, idx, "right"),
        steps=_keep_pairs(_pairs(raw.get("steps"), ("title", "text")), norm, idx, "step"),
        table=_parse_table(
            raw.get("table")
            or {"headers": raw.get("table_headers"), "rows": raw.get("table_rows")},
            norm,
            idx,
        ),
        chart=_parse_chart(raw.get("chart"), norm, idx),
        quote=quote,
        notes=notes,
        footnote=footnote,
        image_prompt=_s(raw.get("image_prompt")),
        uid=idx,
    )
    if has_placeholder(slide.image_prompt):
        slide.image_prompt = ""

    if slide.layout not in LAYOUTS:
        inferred = _infer_layout(slide)
        norm.note("unknown_layout", idx, f"{slide.layout!r} -> {inferred}")
        slide.layout = inferred

    # Layout'ы, у которых не хватает данных, переводим в ближайший осмысленный.
    def downgrade(to: str) -> None:
        norm.note("layout_fallback", idx, f"{slide.layout} -> {to}")
        slide.layout = to

    if slide.layout == "stat":
        # числа, а не слова: нечисловые/длинные значения уходят в пункты; < 2 чисел — не stat
        good = [(v, lab) for v, lab in slide.stats if stat_value_ok(v)]
        for v, lab in slide.stats:
            if not stat_value_ok(v):
                norm.note("stat_value_rejected", idx, v[:30])
                slide.bullets.append(f"{v} — {lab}".strip(" —"))
        slide.stats = good
        if len(good) < 2:
            slide.bullets = [f"{v} — {lab}".strip(" —") for v, lab in good] + slide.bullets
            slide.stats = []
            norm.note("stat_demoted", idx, f"{len(good)} знач.")
            slide.layout = "cards" if len(slide.cards) >= 2 else "bullets"
    if slide.layout == "chart" and not slide.chart:
        downgrade("table" if slide.table else "bullets")
    if slide.layout == "table" and not slide.table:
        downgrade("bullets")
    if slide.layout == "stat" and not slide.stats:
        downgrade("cards" if slide.cards else "bullets")
    if slide.layout == "timeline" and len(slide.steps) < 2:
        if slide.steps:
            slide.bullets += [f"{a}: {b}".strip(": ") for a, b in slide.steps]
            slide.steps = []
        downgrade("bullets")
    if slide.layout == "comparison":
        if len(slide.cards) < 2:
            downgrade(
                "two_column"
                if slide.left and slide.right
                else ("cards" if slide.cards else "bullets")
            )
        else:  # заголовки — cards[i].title, пункты — left/right (по одному на элемент массива)
            bodies = []
            for i, own in enumerate((slide.left, slide.right)):
                pts = own or split_points(slide.cards[i][1])
                if not own and len(pts) > 1:
                    norm.note("comparison_split", idx, f"{len(pts)} пунктов")
                bodies.append(pts)
            slide.left, slide.right = bodies
            slide.cards = [(t, "") for t, _ in slide.cards[:2]]
    if slide.layout == "cards" and not slide.cards:
        downgrade("bullets")
    if slide.layout == "two_column" and not (slide.left or slide.right):
        downgrade("bullets")
    if slide.layout == "quote" and not slide.quote:
        downgrade("bullets")
    if slide.layout == "image" and not slide.image_prompt:
        downgrade("bullets")

    if not slide.title:
        fallback = slide.bullets[0] if slide.bullets else (slide.cards[0][0] if slide.cards else "")
        slide.title = _cut_words(fallback, MAX_TITLE_CHARS) or f"Слайд {idx + 1}"
        norm.note("title_missing", idx)
    if slide.image_prompt and slide.layout not in IMAGE_LAYOUTS:
        norm.note("image_prompt_ignored", idx, slide.layout)
        slide.image_prompt = ""
    if slide.layout not in ("title",) and not slide.notes:
        norm.note("notes_missing", idx)
    if len(slide.bullets) > DECK_MAX_BULLETS:
        norm.note("bullets_over_limit", idx, f"{len(slide.bullets)}")
    return slide


def _move_data_note(subtitle: str, slides: list[Slide], norm: _Norm) -> str:
    """«Условные данные» не место в подзаголовке титульного слайда: пометка уходит в footnote
    первого слайда с данными (диаграмма/таблица/числа), подзаголовок остаётся без неё."""
    parts = re.split(r"\s*[·•|;]\s*|\s+[—–-]\s+", subtitle)
    notes = [p for p in parts if _DATA_NOTE.search(p)]
    if not notes:
        return subtitle
    note = notes[0].strip(" .")
    note = note[:1].upper() + note[1:]
    target = next(
        (s for s in slides if s.layout in ("chart", "table", "stat") and not s.footnote),
        next((s for s in slides if s.layout != "title" and not s.footnote), None),
    )
    if target is not None:
        target.footnote = _cut_words(note, MAX_FOOTNOTE_CHARS)
    norm.note("data_note_moved", detail=note[:40])
    return " · ".join(p for p in parts if p not in notes).strip()


_DATA_LAYOUTS = ("chart", "table", "stat")


def _single_footnote(slides: list[Slide], norm: _Norm) -> None:
    """Одна пометка о данных на колоду и только на первом слайде с данными. Текст короткий
    («Условные данные»): берётся первая фраза."""
    with_note = [s for s in slides if s.footnote]
    if not with_note:
        return
    first = with_note[0].footnote
    clauses = [c for c in re.split(r"\s*[;—–.]\s*", first) if c]
    text = next((c for c in clauses if _DATA_NOTE.search(c)), clauses[0] if clauses else "")
    text = _cut_words(text, 50)
    target = next((s for s in slides if s.layout in _DATA_LAYOUTS), with_note[0])
    for s in with_note:
        if s is not target:
            norm.note("footnote_deduped", s.uid, s.footnote[:40])
        s.footnote = ""
    if text:
        target.footnote = text
        if target is not with_note[0]:
            norm.note("footnote_moved", target.uid, text)


def _fact_plan_ratio(slides: list[Slide]) -> float | None:
    """Выполнение плана в % по диаграмме с рядами «Факт»/«План»; None — нет такой диаграммы
    (или их несколько и они расходятся)."""
    found = set()
    for s in slides:
        if not s.chart or s.chart.type == "pie":
            continue
        by_name = {n.strip().casefold(): v for n, v in s.chart.series}
        fact = next((v for n, v in by_name.items() if n in ("факт", "fact", "actual")), None)
        plan = next((v for n, v in by_name.items() if n in ("план", "plan")), None)
        if fact and plan and sum(plan) > 0:
            found.add(round(sum(fact) / sum(plan) * 100, 4))
    return found.pop() if len(found) == 1 else None


_NUM = r"(\d+(?:[.,]\d+)?)"
# (шаблон, на что указывает число: "pct" — выполнение плана, "over" — превышение над планом)
_PLAN_CLAIMS = (
    (re.compile(rf"(выполнени\w+\s+плана\D{{0,15}}?){_NUM}(\s*%)", re.IGNORECASE), "pct"),
    (
        re.compile(
            rf"(план\w*\s+(?:\w+\s+){{0,3}}?выполнен\w*\s+на\s+){_NUM}(\s*%)", re.IGNORECASE
        ),
        "pct",
    ),
    (re.compile(rf"(перевыполн\w+\s+(?:\w+\s+){{0,2}}?на\s+){_NUM}(\s*%)", re.IGNORECASE), "over"),
)


def _like(old: str, value: float) -> str:
    if "," in old or "." in old:
        return f"{value:.1f}".replace(".", ",") if "," in old else f"{value:.1f}"
    return str(round(value))


def _fix_plan_claims(text: str, pct: float, norm: _Norm, idx: int | None) -> str:
    """Заявленный % выполнения плана сверяем с суммами Факт/План; расхождение > 1 п.п. -> пересчёт."""
    rounded = math.floor(pct + 0.5)
    for pattern, kind in _PLAN_CLAIMS:
        expected = rounded if kind == "pct" else rounded - 100
        if kind == "over" and expected <= 0:
            continue

        def fix(m: re.Match, expected=expected) -> str:
            stated = float(m.group(2).replace(",", "."))
            if abs(stated - expected) <= 1:
                return m.group(0)
            norm.note("numeric_mismatch", idx, f"{m.group(2)}% -> {_like(m.group(2), expected)}%")
            return m.group(1) + _like(m.group(2), expected) + m.group(3)

        text = pattern.sub(fix, text)
    return text


def _check_numbers(slides: list[Slide], norm: _Norm) -> None:
    """Единственный безопасный случай арифметической сверки: заявления о выполнении плана
    рядом с диаграммой «Факт/План». Смысл остальных цифр не проверяем."""
    pct = _fact_plan_ratio(slides)
    if pct is None:
        return
    rounded = math.floor(pct + 0.5)

    def fix(text: str, idx: int) -> str:
        return _fix_plan_claims(text, pct, norm, idx) if text else text

    for s in slides:
        s.title, s.subtitle, s.footnote, s.notes = (
            fix(s.title, s.uid),
            fix(s.subtitle, s.uid),
            fix(s.footnote, s.uid),
            fix(s.notes, s.uid),
        )
        s.bullets = [fix(t, s.uid) for t in s.bullets]
        s.left, s.right = [fix(t, s.uid) for t in s.left], [fix(t, s.uid) for t in s.right]
        s.cards = [(fix(a, s.uid), fix(b, s.uid)) for a, b in s.cards]
        stats = []
        for value, label in s.stats:
            claims_plan = re.search(r"выполнени\w+\s+плана|% плана|плана", label, re.IGNORECASE)
            if claims_plan and re.fullmatch(rf"\s*{_NUM}\s*%", value):
                stated = float(re.sub(r"[^\d.,]", "", value).replace(",", "."))
                if abs(stated - rounded) > 1:
                    norm.note("numeric_mismatch", s.uid, f"{value} -> {rounded}%")
                    value = f"{rounded}%"
            stats.append((value, fix(label, s.uid)))
        s.stats = stats


def _summary_with_note(summary: str, norm: _Norm) -> str:
    """Инструкции вроде «замените фактическими» вычищены со слайдов — они уходят в сообщение чата."""
    if not norm.stats["instruction_stripped"] or re.search(
        r"замен|replace", summary, re.IGNORECASE
    ):
        return summary
    note = (
        "Данные в презентации условные — замените фактическими перед показом."
        if re.search("[А-Яа-я]", summary or "а")
        else "Figures are illustrative — replace them with actual data before presenting."
    )
    return f"{summary} {note}".strip()


def normalize(args: dict[str, Any]) -> Deck:
    """Аргументы tool-вызова модели -> Deck. Никогда не падает на кривых данных."""
    norm = _Norm()
    if not isinstance(args, dict):
        norm.note("args_not_object")
        args = {}

    theme = _s(args.get("theme")).lower()
    if theme not in PALETTES:
        if theme:
            norm.note("unknown_theme", detail=theme)
        theme = DEFAULT_THEME

    slides_in = _jsonish(args.get("slides"), norm, None, "slides")
    raw_slides = slides_in if isinstance(slides_in, list) else []
    if not isinstance(slides_in, list):
        norm.note("slides_not_list")
    if len(raw_slides) > MAX_SLIDES:
        norm.note("slides_over_cap", detail=f"{len(raw_slides)} -> {MAX_SLIDES}")
        raw_slides = raw_slides[:MAX_SLIDES]

    slides = [s for i, raw in enumerate(raw_slides) if (s := _parse_slide(raw, i, norm))]
    title = _s(args.get("title"))
    if has_placeholder(title):
        norm.note("placeholder_dropped", detail=f"deck title: {title[:40]!r}")
        title = ""
    title = _cut_words(title, MAX_TITLE_CHARS)
    if not title:
        title = slides[0].title if slides else "Презентация"
        norm.note("deck_title_missing")

    # Титульный слайд: либо первый слайд с layout=title, либо строится из верхнего уровня.
    hero = _s(args.get("image_prompt"))
    if has_placeholder(hero):
        hero = ""
    subtitle = _s(args.get("subtitle"))
    if has_placeholder(subtitle):
        norm.note("placeholder_dropped", detail=f"deck subtitle: {subtitle[:40]!r}")
        subtitle = ""
    if slides and slides[0].layout == "title":
        first = slides[0]
        if first.image_prompt and not hero:
            hero = first.image_prompt
        first.image_prompt = ""
        subtitle = first.subtitle or subtitle
        first.title = first.title or title
    else:
        slides.insert(0, Slide(layout="title", title=title, subtitle=subtitle, uid=-1))
    for extra in slides[1:]:
        if extra.layout == "title":
            norm.note("extra_title_slide", extra.uid)
            extra.layout = "section"

    subtitle = _move_data_note(subtitle, slides, norm)
    slides[0].subtitle = subtitle
    _single_footnote(slides, norm)
    _check_numbers(slides, norm)
    if len(slides) < MIN_SECTION_DECK:  # разделители нужны только длинной колоде
        kept = [s for s in slides if s.layout != "section"]
        if len(kept) != len(slides):
            norm.note("section_dropped", detail=f"{len(slides) - len(kept)} при {len(slides)} сл.")
            slides = kept

    return Deck(
        title=title,
        subtitle=subtitle,
        theme=theme,
        summary=_summary_with_note(
            _clean_text(_s(args.get("summary")), norm, None, "summary"), norm
        ),
        slides=slides,
        hero_prompt=hero,
        stats=norm.stats,
    )
