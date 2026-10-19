"""Шаблон «ТЗ → Excel» (spec_to_excel).

read_files (+ проверка Excel-шаблона ДО обращения к модели) -> search_links (модель превращает
сырой список в строки JSON, для позиций с брендом/моделью ищет ссылку через веб-поиск;
пачки идут параллельно) -> fill_template (сервер дописывает строки в присланную книгу через
openpyxl, без LLM) -> finalize (загрузка в R2).

Ссылка принимается только если её URL (или домен и путь) встречается среди источников
веб-поиска этого вызова; иначе «[требуется проверка]». Веб-поиск недоступен — «[требуется
веб-поиск]». URL не выдумывается.
"""

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from app.errors import OutputTruncated
from app.routers.document_extract import _extract_docx, _extract_pdf, _extract_xlsx
from app.templates.context import PipelineContext, PipelineResult
from app.templates.errors import (
    GENERATION_FAILED,
    TOO_MANY_ITEMS,
    XLSX_TEMPLATE_INVALID,
    RunError,
)
from app.templates.pipelines.tender_products import BadModelJson, parse_json_object
from app.templates.safety import wrap_document, wrap_items
from app.tools import MIME_XLSX
from app.utils.xlsx_template_filler import (
    SpecRow,
    XlsxTemplateError,
    fill_template,
    inspect_template,
    require_complete,
)

logger = logging.getLogger(__name__)

NEED_CHECK = "[требуется проверка]"
NEED_SEARCH = "[требуется веб-поиск]"
DEFAULT_BATCH_UNITS = 12
DEFAULT_BATCH_CHARS = 8000
DEFAULT_MAX_SEARCHES_PER_BATCH = 30
MAX_URL_LENGTH = 2000
MAX_INPUT_CHARS = 200_000
LINK_STATUSES = ("found", "not_applicable", "not_found", "search_unavailable")

RETRY_SUFFIX = (
    "\n\nПредыдущий ответ не удалось разобрать как валидный JSON. Верни ТОЛЬКО валидный JSON "
    "по схеме из системного промпта, без markdown и пояснений."
)
NO_SEARCH_SUFFIX = (
    "\n\nВеб-поиск недоступен: не пытайся искать. Для позиций с reference_brand_model ставь "
    'link_url = null и link_status = "search_unavailable".'
)

_EXTRACTORS = {"docx": _extract_docx, "pdf": _extract_pdf, "xlsx": _extract_xlsx}
_PARAGRAPHS = re.compile(r"\n\s*\n")


# --- пачки -----------------------------------------------------------------------------------


def make_batches(text: str, max_units: int, max_chars: int) -> list[list[str]]:
    """Делит список на пачки. Единица — абзац (блок через пустую строку); если абзац один —
    строка. Пачка не длиннее max_units единиц и (кроме одиночной длинной единицы) max_chars."""
    paragraphs = [p.strip() for p in _PARAGRAPHS.split(text) if p.strip()]
    units = (
        paragraphs
        if len(paragraphs) > 1
        else [line.strip() for line in text.splitlines() if line.strip()]
    )
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for unit in units:
        if current and (len(current) >= max_units or size + len(unit) > max_chars):
            batches.append(current)
            current, size = [], 0
        current.append(unit)
        size += len(unit)
    if current:
        batches.append(current)
    return batches


# --- ответ модели ---------------------------------------------------------------------------


def _text_or_none(value: Any) -> str | None:
    text = re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""
    return text or None


def _quantity(value: Any) -> int | float:
    if isinstance(value, bool) or value is None:
        return 1
    try:
        number = (
            float(str(value).replace(",", ".").replace(" ", ""))
            if not isinstance(value, (int, float))
            else float(value)
        )
    except ValueError:
        return 1
    if number <= 0:
        return 1
    return int(number) if number.is_integer() else number


def validate_rows(data: dict) -> list[dict]:
    raw_rows = data.get("rows")
    if not isinstance(raw_rows, list):
        raise BadModelJson("нет списка rows")
    rows = []
    for raw in raw_rows:
        if not isinstance(raw, dict) or not _text_or_none(raw.get("name")):
            raise BadModelJson("строка без наименования")
        brand = _text_or_none(raw.get("reference_brand_model"))
        status = str(raw.get("link_status") or "").strip().lower()
        if status not in LINK_STATUSES:
            status = "not_found" if brand else "not_applicable"
        rows.append(
            {
                "name": _text_or_none(raw["name"]),
                "specification": _text_or_none(raw.get("specification")) or "",
                "unit": _text_or_none(raw.get("unit")) or "шт",
                "quantity": _quantity(raw.get("quantity")),
                "section": _text_or_none(raw.get("section")),
                "reference_brand_model": brand,
                "link_url": _text_or_none(raw.get("link_url")),
                "link_status": status,
            }
        )
    return rows


# --- проверка ссылок ------------------------------------------------------------------------


def _host_path(url: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower().removeprefix("www.")
    return host, parts.path.rstrip("/").lower()


def url_in_sources(url: str, sources: list[str]) -> bool:
    """URL (или его домен и путь) встречается среди источников веб-поиска."""
    if len(url) > MAX_URL_LENGTH:
        return False
    target = _host_path(url)
    if target is None:
        return False
    for source in sources:
        known = _host_path(source)
        if known is None or known[0] != target[0]:
            continue
        a, b = target[1], known[1]
        if a == b:
            return True
        short, long = sorted((a, b), key=len)
        if short and long.startswith(short + "/"):  # один путь — продолжение другого
            return True
    return False


def link_cell(row: dict, sources: list[str], degraded: bool) -> tuple[str | None, str | None]:
    """(url для гиперссылки, текст-заглушка). Нет бренда — ячейка пустая."""
    if not row["reference_brand_model"]:
        return None, None
    if degraded or row["link_status"] == "search_unavailable":
        return None, NEED_SEARCH
    url = row["link_url"]
    if url and url_in_sources(url, sources):
        return url, None
    return None, NEED_CHECK


# --- вызовы модели --------------------------------------------------------------------------


async def _call_rows(
    ctx: PipelineContext, system: str, user: str, *, search: bool, max_uses: int
) -> tuple[list[dict], list[str]]:
    """Один запрос (+ один повтор при невалидном JSON). Возвращает (строки, URL источников)."""
    last: BadModelJson | None = None
    for attempt in range(2):
        result = await ctx.generate(
            system=system,
            user=user + (RETRY_SUFFIX if attempt else "") + ("" if search else NO_SEARCH_SUFFIX),
            stage="search_links",
            web_search=search,
            web_search_max_uses=max_uses if search else None,
        )
        try:
            return validate_rows(parse_json_object(result.text)), [s.url for s in result.sources]
        except BadModelJson as exc:
            last = exc
            logger.warning("run %s: bad rows JSON (attempt %d): %s", ctx.run_id, attempt + 1, exc)
    raise RunError(
        GENERATION_FAILED, "Модель вернула некорректный ответ. Попробуйте ещё раз."
    ) from last


async def _process_batch(
    ctx: PipelineContext, system: str, units: list[str], wrap, max_uses: int
) -> list[dict]:
    """Пачка -> строки. Оборванный ответ делит пачку пополам; сбой поиска — повтор без поиска
    (ссылки тогда «[требуется веб-поиск]»)."""
    user = wrap("\n\n".join(units))
    degraded = False
    try:
        try:
            rows, sources = await _call_rows(ctx, system, user, search=True, max_uses=max_uses)
        except (OutputTruncated, RunError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 — провайдер/поиск недоступен
            logger.warning(
                "run %s: search call failed (%r), retrying without search", ctx.run_id, exc
            )
            degraded = True
            rows, sources = await _call_rows(ctx, system, user, search=False, max_uses=0)
    except OutputTruncated:
        if len(units) < 2:
            raise
        mid = len(units) // 2
        ctx.add_warning("Ответ модели не поместился целиком: часть списка обработана по частям.")
        parts = await asyncio.gather(
            _process_batch(ctx, system, units[:mid], wrap, max_uses),
            _process_batch(ctx, system, units[mid:], wrap, max_uses),
        )
        return parts[0] + parts[1]
    for row in rows:
        row["url_checked"], row["link_note"] = link_cell(row, sources, degraded)
        row["degraded"] = degraded
    return rows


async def _read_items_file(ctx: PipelineContext) -> tuple[str, str] | None:
    items_file = await ctx.file("items_file")
    if items_file is None:
        return None
    extractor = _EXTRACTORS.get(items_file.kind)
    if extractor is None:
        raise RunError(GENERATION_FAILED, f"Формат файла «{items_file.name}» не поддерживается.")
    try:
        text = await asyncio.to_thread(extractor, items_file.data)
    except Exception as exc:
        logger.warning("run %s: items file read failed: %r", ctx.run_id, exc)
        raise RunError(
            GENERATION_FAILED, f"Не удалось прочитать файл «{items_file.name}»."
        ) from exc
    if not text.strip():
        raise RunError(
            GENERATION_FAILED,
            f"В файле «{items_file.name}» нет текста (возможно, это скан). "
            "Вставьте список товаров текстом.",
        )
    if len(text) > MAX_INPUT_CHARS:
        text = text[:MAX_INPUT_CHARS]
        ctx.add_warning(
            f"Файл «{items_file.name}» слишком большой: обработано {MAX_INPUT_CHARS} символов."
        )
    return items_file.name, text


async def run(ctx: PipelineContext) -> PipelineResult:
    spec = ctx.spec
    max_units = int(spec.settings.get("batch_units", DEFAULT_BATCH_UNITS))
    max_chars = int(spec.settings.get("batch_chars", DEFAULT_BATCH_CHARS))
    batch_cap = int(spec.settings.get("max_searches_per_batch", DEFAULT_MAX_SEARCHES_PER_BATCH))

    await ctx.progress("read_files", 0, 1, "Чтение файлов")
    template = await ctx.file("template_xlsx")
    if template is None:
        raise RunError(XLSX_TEMPLATE_INVALID, "Не приложен Excel-шаблон.")
    # Шаблон проверяем ДО обращения к модели: непригодный шаблон не должен стоить денег.
    try:
        layout = await asyncio.to_thread(inspect_template, template.data)
        require_complete(layout)
    except XlsxTemplateError as exc:
        raise RunError(XLSX_TEMPLATE_INVALID, str(exc)) from exc
    for warning in layout.warnings:
        ctx.add_warning(warning)

    sources_text: list[tuple[list[list[str]], Any]] = []
    items_text = (ctx.params.get("items_text") or "").strip()
    if items_text:
        sources_text.append((make_batches(items_text, max_units, max_chars), wrap_items))
    from_file = await _read_items_file(ctx)
    if from_file:
        name, text = from_file
        sources_text.append(
            (
                make_batches(text, max_units, max_chars),
                lambda body, n=name: wrap_document(n, body),
            )
        )
    jobs = [(units, wrap) for batches, wrap in sources_text for units in batches]
    if not jobs:
        raise RunError(GENERATION_FAILED, "Список товаров пуст.")

    system = spec.read_prompt("system.md")
    per_item = ctx.limits.max_searches_per_item
    total = len(jobs)
    done = 0

    async def one(units: list[str], wrap) -> list[dict]:
        nonlocal done
        # Сколько позиций с брендом — известно только после ответа: берём верхнюю границу.
        max_uses = max(1, min(len(units) * per_item, batch_cap))
        rows = await _process_batch(ctx, system, units, wrap, max_uses)
        done += 1
        await ctx.progress(
            "search_links", done, total, f"Обработано частей списка: {done} из {total}"
        )
        return rows

    await ctx.progress("search_links", 0, total, f"Обработано частей списка: 0 из {total}")
    batches_rows = await asyncio.gather(*(one(units, wrap) for units, wrap in jobs))
    rows = [row for batch in batches_rows for row in batch]

    if not rows:
        raise RunError(GENERATION_FAILED, "Из списка не удалось выделить ни одной позиции.")
    if len(rows) > ctx.limits.max_items:
        raise RunError(
            TOO_MANY_ITEMS,
            f"Найдено позиций: {len(rows)}, лимит: {ctx.limits.max_items}. "
            "Разбейте список на части.",
        )

    await ctx.progress("fill_template", 0, 1, "Заполнение шаблона")
    spec_rows = [
        SpecRow(
            name=r["name"],
            specification=r["specification"],
            unit=r["unit"],
            quantity=r["quantity"],
            section=r["section"],
            link_url=r["url_checked"],
            link_note=r["link_note"],
        )
        for r in rows
    ]
    try:
        filled = await asyncio.to_thread(
            fill_template, template.data, spec_rows, ctx.params.get("section")
        )
    except XlsxTemplateError as exc:
        raise RunError(XLSX_TEMPLATE_INVALID, str(exc)) from exc

    with_brand = [(i, r) for i, r in enumerate(rows) if r["reference_brand_model"]]
    found = sum(1 for _, r in with_brand if r["url_checked"])
    unchecked = [(i, r) for i, r in with_brand if r["link_note"] == NEED_CHECK]
    no_search = [(i, r) for i, r in with_brand if r["link_note"] == NEED_SEARCH]
    for i, r in unchecked:
        ctx.add_warning(
            f"Строка {filled.first_row + i}: ссылка для «{r['reference_brand_model']}» не "
            "подтверждена результатами поиска — требуется проверка."
        )
    if no_search:
        ctx.add_warning(
            f"Веб-поиск был недоступен: ссылки не получены для строк "
            f"{', '.join(str(filled.first_row + i) for i, _ in no_search[:20])}"
            f"{' и других' if len(no_search) > 20 else ''}."
        )
    if with_brand and ctx.searches_used == 0 and not no_search:
        ctx.add_warning("Веб-поиск не использовался: ссылки не проверены.")

    await ctx.progress("finalize", 0, 1, "Сохранение файла")
    stem = template.name.rsplit(".", 1)[0]
    attachment = await ctx.upload_output(f"{stem}_заполнено.xlsx", filled.data, MIME_XLSX)
    await ctx.progress("finalize", 1, 1, "Готово")

    review = len(unchecked) + len(no_search)
    return PipelineResult(
        text=f"Добавлено строк: {len(rows)}, ссылок найдено: {found}, требуют проверки: {review}",
        table=None,
        attachments=[attachment],
        stats={
            "items_found": len(rows),
            "items_done": len(rows) - review,
            "items_failed": review,
        },
    )
