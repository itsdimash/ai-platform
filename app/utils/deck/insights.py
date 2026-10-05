"""Выводы по данным диаграммы — считаются кодом по числам (максимум, минимум, изменение,
доли), без обращения к модели. Язык (ru/en) определяется по заголовку и категориям."""

import re

from app.utils.deck.schema import Chart

_CYR = re.compile(r"[А-Яа-яЁё]")

_T = {
    "ru": {
        "max": "Максимум: {cat} — {val}",
        "min": "Минимум: {cat} — {val}",
        "change": "От «{a}» к «{b}»: {delta} ({pct})",
        "top": "Крупнейшая доля: {cat} — {pct}",
        "low": "Наименьшая доля: {cat} — {pct}",
        "top2": "Две крупнейшие вместе — {pct}",
        "series": " ({name})",
    },
    "en": {
        "max": "Highest: {cat} — {val}",
        "min": "Lowest: {cat} — {val}",
        "change": "From “{a}” to “{b}”: {delta} ({pct})",
        "top": "Largest share: {cat} — {pct}",
        "low": "Smallest share: {cat} — {pct}",
        "top2": "Top two combined — {pct}",
        "series": " ({name})",
    },
}


def _num(v: float, ru: bool, signed: bool = False) -> str:
    text = f"{abs(v):,.0f}" if float(round(v, 1)).is_integer() else f"{abs(v):,.1f}"
    text = text.replace(",", " ") if ru else text
    if ru:
        text = text.replace(".", ",")
    sign = "+" if v > 0 and signed else ("−" if v < 0 else "")
    return f"{sign}{text}"


def _val(v: float, unit: str, ru: bool, signed: bool = False) -> str:
    text = _num(v, ru, signed)
    if not unit:
        return text
    return f"{text}{unit}" if unit == "%" else f"{text} {unit}"


def _pct(v: float, ru: bool, signed: bool = False) -> str:
    return f"{_num(v, ru, signed)}%"


def chart_takeaways(chart: Chart, title: str = "") -> list[str]:
    """2-3 коротких вывода по диаграмме; пусто, если данных недостаточно."""
    ru = bool(_CYR.search(" ".join([title, *chart.categories])))
    t = _T["ru" if ru else "en"]
    cats = chart.categories
    name, values = chart.series[0]
    if len(values) < 2:
        return []
    if chart.type == "pie":
        total = sum(values)
        if total <= 0:
            return []
        order = sorted(range(len(values)), key=lambda i: values[i], reverse=True)
        share = lambda i: values[i] / total * 100
        out = [t["top"].format(cat=cats[order[0]], pct=_pct(share(order[0]), ru))]
        if len(order) > 2:
            out.append(t["low"].format(cat=cats[order[-1]], pct=_pct(share(order[-1]), ru)))
            out.append(t["top2"].format(pct=_pct(share(order[0]) + share(order[1]), ru)))
        return out[:3]
    multi = len(chart.series) > 1
    tag = t["series"].format(name=name) if multi else ""
    hi = max(range(len(values)), key=lambda i: values[i])
    lo = min(range(len(values)), key=lambda i: values[i])
    out = [t["max"].format(cat=cats[hi], val=_val(values[hi], chart.unit, ru)) + tag]
    if values[lo] != values[hi]:
        out.append(t["min"].format(cat=cats[lo], val=_val(values[lo], chart.unit, ru)) + tag)
    first, last = values[0], values[-1]
    if last != first:
        pct = _pct((last - first) / abs(first) * 100, ru, True) if first else ""
        delta = _val(last - first, chart.unit, ru, True)
        out.append(
            t["change"].format(a=cats[0], b=cats[-1], delta=delta, pct=pct or "—") + tag
            if pct
            else f"{cats[0]} → {cats[-1]}: {delta}" + tag
        )
    return out[:3]
