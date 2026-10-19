"""Исполнитель запусков шаблонов: asyncio-задачи внутри процесса ai-platform.

HTTP-запрос только создаёт run (status=queued) и отвечает 202; задача выполняется в фоне.
Состояние — в таблице template_runs (через RunStore). Один процесс uvicorn: ссылки на задачи
хранятся в памяти процесса (нужны для отмены); при перезапуске незавершённые run получают
failed/interrupted (recover)."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from app.adapters.base import ModelAdapter
from app.auth import CurrentUser
from app.errors import GenerationError, OutputTruncated
from app.router.route import load_config
from app.templates import inputs
from app.templates.context import PipelineContext, PipelineResult
from app.templates.errors import (
    GENERATION_FAILED,
    GENERIC_FAILURE_MESSAGE,
    OUTPUT_TRUNCATED,
    TIMEOUT,
    RunError,
    TemplateHTTPError,
)
from app.templates.pipelines import resolve_pipeline
from app.templates.spec import TemplateRegistry, TemplateSpec
from app.templates.storage import TemplateStorage
from app.templates.store import ACTIVE, RunRecord, RunStore, utcnow

logger = logging.getLogger(__name__)

MAX_WARNINGS_IN_TEXT = 10
DEFAULT_GLOBAL_LLM_CONCURRENCY = 8
CANCEL_WAIT_S = 10.0


def warnings_block(warnings: list[str]) -> str:
    """Блок «Предупреждения:» для текста сообщения (история хранит только текст)."""
    if not warnings:
        return ""
    shown = warnings[:MAX_WARNINGS_IN_TEXT]
    lines = ["Предупреждения:", *(f"- {w}" for w in shown)]
    if len(warnings) > len(shown):
        lines.append(f"- и ещё {len(warnings) - len(shown)}")
    return "\n".join(lines)


class RunManager:
    def __init__(
        self,
        *,
        registry: TemplateRegistry,
        store: RunStore,
        storage: TemplateStorage,
        adapters_provider: Callable[[], dict[str, ModelAdapter]],
        global_llm_concurrency: int | None = None,
        progress_interval: float = 1.0,
    ):
        self.registry = registry
        self.store = store
        self.storage = storage
        self._adapters_provider = adapters_provider
        if global_llm_concurrency is None:
            global_llm_concurrency = load_config()["limits"].get(
                "template_llm_concurrency", DEFAULT_GLOBAL_LLM_CONCURRENCY
            )
        self._global_sem = asyncio.Semaphore(global_llm_concurrency)
        self._progress_interval = progress_interval
        self._tasks: dict[str, asyncio.Task] = {}
        self._contexts: dict[str, PipelineContext] = {}
        self._create_lock = asyncio.Lock()

    # --- запуск --------------------------------------------------------------------------

    async def start_run(
        self,
        user: CurrentUser,
        template_id: str,
        *,
        files: dict[str, Any] | None,
        params: dict[str, Any] | None,
        session_id: int | None,
    ) -> RunRecord:
        spec = self.registry.get(template_id)
        if spec is None:
            raise TemplateHTTPError(404, "template_not_found", "Шаблон не найден")
        if user.role not in spec.allowed_roles:
            raise TemplateHTTPError(403, "template_forbidden", "Шаблон недоступен для вашей роли")

        keys_by_slot, clean_params = inputs.validate_shape(spec, files, params)
        records = await inputs.resolve_files(spec, user, keys_by_slot, self.storage)

        async with self._create_lock:  # проверка лимита и создание — атомарно в процессе
            if await self.store.count_active(user.user_id) >= spec.limits.max_runs_per_user:
                raise TemplateHTTPError(
                    429,
                    "run_limit_exceeded",
                    f"Уже выполняется запусков: {spec.limits.max_runs_per_user}. "
                    "Дождитесь завершения или отмените один из них.",
                )
            record = await self.store.create(
                user_id=user.user_id,
                user_role=user.role,
                template_id=spec.id,
                session_id=session_id,
                session_title=f"Шаблон «{spec.title}»",
                user_content=inputs.format_user_message(spec, clean_params),
                user_attachments=[r for slot in spec.files for r in records.get(slot.slot, [])],
                params=clean_params,
                input_files=records,
            )
        task = asyncio.create_task(self._execute(record, spec), name=f"run-{record.id}")
        self._tasks[record.id] = task
        task.add_done_callback(lambda _t, rid=record.id: self._tasks.pop(rid, None))
        return record

    # --- чтение и отмена -----------------------------------------------------------------

    async def get_run(self, user: CurrentUser, run_id: str) -> RunRecord:
        record = await self.store.get(run_id)
        if record is None or not (record.user_id == user.user_id or user.role == "admin"):
            raise TemplateHTTPError(404, "run_not_found", "Запуск не найден")
        return record

    async def cancel_run(self, user: CurrentUser, run_id: str) -> RunRecord:
        record = await self.get_run(user, run_id)
        if record.status not in ACTIVE:
            return record
        task = self._tasks.get(record.id)
        ctx = self._contexts.get(record.id)
        if ctx is not None:
            ctx.cancelled = True
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=CANCEL_WAIT_S)
        # Задача отменена до первого шага (тело корутины не выполнялось) или её нет в этом
        # процессе — закрываем запись сами. finish() идемпотентен для терминальных статусов.
        current = await self.store.get(record.id)
        if current is not None and current.status in ACTIVE:
            await self._finish_cancelled(current, self.registry.get(record.template_id), ctx)
        return await self.get_run(user, run_id)

    async def recover(self) -> int:
        """При старте: queued/running -> failed (interrupted)."""

        def message_for(template_id: str) -> str:
            spec = self.registry.get(template_id)
            title = spec.title if spec else template_id
            return f"Запуск шаблона «{title}» прерван перезапуском сервиса."

        count = await self.store.mark_interrupted(message_for)
        if count:
            logger.warning("marked %d interrupted template runs", count)
        return count

    async def wait(self, run_id: str) -> None:
        """Ждёт завершения задачи (тесты)."""
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.wait({task})

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()

    # --- выполнение ----------------------------------------------------------------------

    async def _execute(self, record: RunRecord, spec: TemplateSpec) -> None:
        run_id = record.id
        user = CurrentUser(user_id=record.user_id, role=record.user_role)

        async def write_progress(progress: dict) -> None:
            await self.store.update(run_id, progress=progress)

        ctx = PipelineContext(
            run_id=run_id,
            spec=spec,
            user=user,
            session_id=record.session_id,
            params=record.params,
            input_files=record.input_files,
            adapters=self._adapters_provider(),
            storage=self.storage,
            write_progress=write_progress,
            log_request=self.store.log_request,
            global_semaphore=self._global_sem,
            progress_interval=self._progress_interval,
        )
        self._contexts[run_id] = ctx
        try:
            await self.store.update(
                run_id,
                status="running",
                started_at=utcnow(),
                progress={"stage": "read_files", "done": 0, "total": 0, "message": "Запуск"},
            )
            pipeline = resolve_pipeline(spec.pipeline)
            timeout = asyncio.timeout(spec.limits.run_timeout_s)
            try:
                async with timeout:
                    outcome = await pipeline(ctx)
            except TimeoutError:
                if not timeout.expired():
                    raise
                await self._finish_failed(
                    record,
                    spec,
                    ctx,
                    TIMEOUT,
                    f"Превышено время выполнения ({spec.limits.run_timeout_s} с)",
                )
                return
            await ctx.flush_progress()
            await self._finish_done(record, spec, ctx, outcome)
        except asyncio.CancelledError:
            await self._finish_cancelled(record, spec, ctx)
        except RunError as exc:
            await self._finish_failed(record, spec, ctx, exc.code, exc.message)
        except OutputTruncated as exc:
            logger.warning("run %s truncated: %s", run_id, exc.internal)
            await self._finish_failed(record, spec, ctx, OUTPUT_TRUNCATED, exc.detail["message"])
        except GenerationError as exc:
            logger.warning("run %s generation error: %s", run_id, exc.internal)
            await self._finish_failed(record, spec, ctx, GENERATION_FAILED, exc.detail["message"])
        except Exception:
            # Подробности — только в серверный лог; пользователю — общее сообщение.
            logger.exception("run %s failed", run_id)
            await self._finish_failed(record, spec, ctx, GENERATION_FAILED, GENERIC_FAILURE_MESSAGE)
        finally:
            self._contexts.pop(run_id, None)

    def _totals(self, ctx: PipelineContext | None) -> dict:
        if ctx is None:
            return {"tokens_in": 0, "tokens_out": 0, "searches_used": 0}
        return {
            "tokens_in": ctx.tokens_in,
            "tokens_out": ctx.tokens_out,
            "searches_used": ctx.searches_used,
        }

    async def _finish_done(
        self, record: RunRecord, spec: TemplateSpec, ctx: PipelineContext, outcome: PipelineResult
    ) -> None:
        totals = self._totals(ctx)
        stats = {
            **outcome.stats,
            "searches": ctx.searches_used,
            "tokens_in": ctx.tokens_in,
            "tokens_out": ctx.tokens_out,
        }
        result = {
            "text": outcome.text,
            "table": outcome.table,
            "attachments": [{**a, "url": None} for a in outcome.attachments],
            "warnings": list(ctx.warnings),
            "sources": [{"url": s.url, "title": s.title} for s in ctx.sources],
            "stats": stats,
        }
        block = warnings_block(ctx.warnings)
        text = f"{outcome.text}\n\n{block}" if block else outcome.text
        await self.store.finish(
            record.id,
            status="done",
            result=result,
            error=None,
            assistant_text=text,
            table=outcome.table,
            attachments=outcome.attachments,
            model_used=(ctx.model_used or "")[:50] or None,
            task_type=f"template:{spec.id}",
            **totals,
        )

    async def _finish_failed(
        self,
        record: RunRecord,
        spec: TemplateSpec,
        ctx: PipelineContext | None,
        code: str,
        message: str,
    ) -> None:
        await self.store.finish(
            record.id,
            status="failed",
            result=None,
            error={"code": code, "message": message},
            assistant_text=f"Не удалось выполнить шаблон «{spec.title}»: {message}",
            table=None,
            attachments=[],
            model_used=(ctx.model_used if ctx else None),
            task_type=f"template:{spec.id}",
            **self._totals(ctx),
        )

    async def _finish_cancelled(
        self, record: RunRecord, spec: TemplateSpec | None, ctx: PipelineContext | None
    ) -> None:
        title = spec.title if spec else record.template_id
        await self.store.finish(
            record.id,
            status="cancelled",
            result=None,
            error=None,
            assistant_text=f"Запуск шаблона «{title}» отменён.",
            table=None,
            attachments=[],
            model_used=(ctx.model_used if ctx else None),
            task_type=f"template:{record.template_id}",
            **self._totals(ctx),
        )


_manager: RunManager | None = None


def get_run_manager() -> RunManager:
    """Процессный синглтон; FastAPI-зависимость (в тестах переопределяется)."""
    global _manager
    if _manager is None:
        from app.routers.deps import get_adapters
        from app.templates.spec import get_registry
        from app.templates.storage import R2Storage
        from app.templates.store import PgRunStore

        _manager = RunManager(
            registry=get_registry(),
            store=PgRunStore(),
            storage=R2Storage(),
            adapters_provider=get_adapters,
        )
    return _manager


__all__ = ["RunManager", "get_run_manager", "warnings_block"]
