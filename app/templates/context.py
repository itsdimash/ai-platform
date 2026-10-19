"""PipelineContext — всё, что нужно пайплайну: входные файлы, прогресс, вызовы модели,
сохранение результата. Пайплайны НЕ вызывают адаптеры напрямую: ctx.generate обязан
применять strict_finish, писать ai_request_logs и копить sources/searches/токены."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from app.adapters.base import ChatTurn, GenerationResult, ModelAdapter, SourceRef, add_source
from app.auth import CurrentUser
from app.limits import MAX_DOCUMENT_BYTES
from app.router.route import load_config
from app.services.chat_service import _check_finish
from app.templates.errors import GENERATION_FAILED, RunError
from app.templates.spec import TemplateSpec
from app.templates.storage import TemplateStorage
from app.utils.storage import build_key, display_name, make_record
from app.utils.uploads import classify_upload

logger = logging.getLogger(__name__)


def provider_timeout_s() -> int:
    return load_config()["limits"]["provider_timeout_s"]


PROGRESS_INTERVAL_S = 1.0
STAGES = (
    "queued",
    "read_files",
    "extract_cell",
    "split_items",
    "search_brands",
    "search_links",
    "fill_template",
    "finalize",
)


@dataclass
class InputFile:
    name: str
    key: str
    mime: str
    kind: str  # image | pdf | docx | xlsx
    data: bytes


@dataclass
class PipelineResult:
    text: str
    table: list[dict] | None = None
    attachments: list[dict] = field(default_factory=list)  # записи из ctx.upload_output
    stats: dict[str, Any] = field(default_factory=dict)  # items_found/items_done/items_failed...


ProgressWriter = Callable[[dict], Awaitable[None]]
RequestLogger = Callable[..., Awaitable[None]]


class PipelineContext:
    def __init__(
        self,
        *,
        run_id: str,
        spec: TemplateSpec,
        user: CurrentUser,
        session_id: int | None,
        params: dict[str, Any],
        input_files: dict[str, list[dict]],
        adapters: dict[str, ModelAdapter],
        storage: TemplateStorage,
        write_progress: ProgressWriter,
        log_request: RequestLogger,
        global_semaphore: asyncio.Semaphore,
        progress_interval: float = PROGRESS_INTERVAL_S,
    ):
        self.run_id = run_id
        self.spec = spec
        self.user = user
        self.session_id = session_id
        self.params = params
        self.limits = spec.limits
        self._input_files = input_files
        self._adapters = adapters
        self._storage = storage
        self._write_progress = write_progress
        self._log_request = log_request
        self._global_sem = global_semaphore
        self._local_sem = asyncio.Semaphore(spec.limits.concurrency)
        self._progress_interval = progress_interval
        self._last_progress_at = float("-inf")
        self._latest_progress: dict | None = None
        self._progress_dirty = False
        self._file_cache: dict[str, list[InputFile]] = {}
        self.cancelled = False

        self.warnings: list[str] = []
        self.sources: list[SourceRef] = []
        self.searches_used = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.model_used: str | None = None

    # --- служебное -----------------------------------------------------------------------

    def is_cancelled(self) -> bool:
        return self.cancelled

    def add_warning(self, text: str) -> None:
        if text and text not in self.warnings:
            self.warnings.append(text)

    # --- прогресс ------------------------------------------------------------------------

    async def progress(self, stage: str, done: int = 0, total: int = 0, message: str = "") -> None:
        """Обновляет прогресс; запись в БД — не чаще раза в PROGRESS_INTERVAL_S."""
        self._latest_progress = {
            "stage": stage,
            "done": done,
            "total": total,
            "message": message,
        }
        now = time.monotonic()
        if now - self._last_progress_at >= self._progress_interval:
            self._last_progress_at = now
            self._progress_dirty = False
            await self._write_progress(self._latest_progress)
        else:
            self._progress_dirty = True

    async def flush_progress(self) -> None:
        if self._progress_dirty and self._latest_progress is not None:
            self._progress_dirty = False
            self._last_progress_at = time.monotonic()
            await self._write_progress(self._latest_progress)

    # --- входные файлы -------------------------------------------------------------------

    async def files(self, slot: str) -> list[InputFile]:
        """Файлы слота (читаются из R2 лениво и кэшируются). Проверка по magic bytes."""
        if slot in self._file_cache:
            return self._file_cache[slot]
        slot_spec = next((f for f in self.spec.files if f.slot == slot), None)
        if slot_spec is None:
            raise KeyError(f"неизвестный слот {slot!r}")
        loaded: list[InputFile] = []
        for record in self._input_files.get(slot, []):
            data = await self._storage.read(record["key"])
            if len(data) > MAX_DOCUMENT_BYTES:
                raise RunError(GENERATION_FAILED, f"Файл «{record['name']}» слишком большой")
            try:
                parsed = classify_upload(
                    record["name"], data, allowed_exts=frozenset(slot_spec.accept)
                )
            except HTTPException as exc:
                raise RunError(
                    GENERATION_FAILED, f"Файл «{record['name']}» не прочитан: {exc.detail}"
                ) from exc
            loaded.append(InputFile(record["name"], record["key"], parsed.mime, parsed.kind, data))
        self._file_cache[slot] = loaded
        return loaded

    async def file(self, slot: str) -> InputFile | None:
        found = await self.files(slot)
        return found[0] if found else None

    # --- результат -----------------------------------------------------------------------

    async def upload_output(self, name: str, data: bytes, mime: str) -> dict:
        """Кладёт файл результата в R2 под ai/{user_id}/{session_id}/… и возвращает запись
        вложения (формат БД, без url)."""
        shown = display_name(name, default="result")
        key = build_key(self.user.user_id, self.session_id, shown)
        await self._storage.write(key, data, mime, shown)
        return make_record(name=shown, key=key, mime=mime, size=len(data))

    # --- вызов модели --------------------------------------------------------------------

    async def generate(
        self,
        *,
        system: str,
        user: str,
        stage: str | None = None,
        web_search: bool = False,
        web_search_max_uses: int | None = None,
        max_tokens: int | None = None,
        thinking: str | None = None,
    ) -> GenerationResult:
        """Один вызов модели шаблона. Всегда: strict_finish (обрыв по max_tokens —
        OutputTruncated), запись в ai_request_logs (template_id, run_id, searches_used),
        накопление sources/searches_used/токенов в run. Превышение лимита поисков — warning."""
        model_name = self.spec.model_for(stage)
        adapter = self._adapters[model_name]
        limit = min(max_tokens or self.spec.max_tokens, adapter.caps.max_output_tokens)
        started = time.monotonic()
        result: GenerationResult | None = None
        try:
            async with self._local_sem, self._global_sem:
                result = await adapter.generate(
                    messages=[ChatTurn("user", user)],
                    system=system,
                    web_search=web_search,
                    web_search_max_uses=web_search_max_uses if web_search else None,
                    max_tokens=limit,
                    tools=[],  # файловые инструменты чата шаблонам не нужны
                    thinking=thinking or self.spec.thinking,
                    timeout_s=min(self.limits.run_timeout_s, provider_timeout_s()),
                )
            self._account(result)
            _check_finish(result, tool_expected=False, strict_finish=True)
        except Exception as exc:
            await self._log(
                model=result.model_used if result else None,
                adapter=adapter,
                result=result,
                started=started,
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
            raise
        if (
            web_search
            and web_search_max_uses is not None
            and result.searches_used > web_search_max_uses
        ):
            self.add_warning(
                f"Поисков выполнено больше лимита: {result.searches_used} из {web_search_max_uses}"
            )
        await self._log(
            model=result.model_used, adapter=adapter, result=result, started=started, error=None
        )
        return result

    def _account(self, result: GenerationResult) -> None:
        self.tokens_in += result.tokens_in
        self.tokens_out += result.tokens_out
        self.searches_used += result.searches_used
        self.model_used = result.model_used or self.model_used
        for src in result.sources:
            add_source(self.sources, src.url, src.title)

    async def _log(self, *, model, adapter, result, started, error) -> None:
        try:
            await self._log_request(
                user_id=self.user.user_id,
                session_id=self.session_id,
                task_type=f"template:{self.spec.id}",
                confidence=1.0,
                used_fallback_confidence=False,
                model_used=(model or adapter.model)[:50],
                tokens_in=result.tokens_in if result else 0,
                tokens_out=result.tokens_out if result else 0,
                latency_ms=int((time.monotonic() - started) * 1000),
                template_id=self.spec.id,
                run_id=self.run_id,
                searches_used=result.searches_used if result else 0,
                status="error" if error else "success",
                error_message=error,
            )
        except Exception:
            logger.warning("failed to log template request", exc_info=True)
