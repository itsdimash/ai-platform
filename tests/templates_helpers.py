"""Заглушки для тестов шаблонов: память вместо БД и R2, тестовый шаблон _echo."""

import asyncio
import uuid
from pathlib import Path
from typing import ClassVar

from app.templates.context import PipelineContext, PipelineResult
from app.templates.pipelines import register_pipeline, unregister_pipeline
from app.templates.runner import RunManager
from app.templates.spec import load_registry
from app.templates.store import ACTIVE, TERMINAL, RunRecord, RunStore, utcnow
from app.utils.docx_builder import build_document
from tests.helpers import fake_adapters

DOCX = build_document("Тест", [{"paragraphs": ["Первый абзац", "Второй абзац"]}])
USER_KEY = "ai/12/_pending/uploads/ab12cd34ab12cd34ab12cd34ab12cd34_input.docx"
OTHER_KEY = "ai/77/_pending/uploads/ab12cd34ab12cd34ab12cd34ab12cd34_input.docx"


class MemoryStorage:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str, str]] = {}

    def put(self, key: str, data: bytes, mime="application/octet-stream", name=None):
        self.objects[key] = (data, mime, name or key.rsplit("/", 1)[-1])

    async def head(self, key):
        obj = self.objects.get(key)
        if obj is None:
            return None
        return {"size": len(obj[0]), "mime": obj[1], "name": obj[2].split("_", 1)[-1]}

    async def read(self, key):
        return self.objects[key][0]

    async def write(self, key, data, mime, name):
        self.objects[key] = (data, mime, name)


class MemoryRunStore(RunStore):
    def __init__(self):
        self.runs: dict[str, RunRecord] = {}
        self.messages: list[dict] = []
        self.logs: list[dict] = []
        self.sessions: dict[int, int] = {}
        self.progress_writes = 0

    async def create(
        self,
        *,
        user_id,
        user_role,
        template_id,
        session_id,
        session_title,
        user_content,
        user_attachments,
        params,
        input_files,
    ):
        from app.templates.errors import TemplateHTTPError

        if session_id is not None:
            if self.sessions.get(session_id) != user_id:
                raise TemplateHTTPError(404, "session_not_found", "Сессия не найдена")
        else:
            session_id = len(self.sessions) + 100
            self.sessions[session_id] = user_id
        self.messages.append(
            {
                "session_id": session_id,
                "role": "user",
                "content": user_content,
                "attachments": user_attachments,
            }
        )
        rec = RunRecord(
            id=str(uuid.uuid4()),
            user_id=user_id,
            user_role=user_role,
            template_id=template_id,
            session_id=session_id,
            status="queued",
            progress={"stage": "queued", "done": 0, "total": 0, "message": "В очереди"},
            params=params,
            input_files=input_files,
        )
        self.runs[rec.id] = rec
        return rec

    async def get(self, run_id):
        rec = self.runs.get(run_id)
        return None if rec is None else RunRecord(**rec.__dict__)

    async def count_active(self, user_id):
        return sum(1 for r in self.runs.values() if r.user_id == user_id and r.status in ACTIVE)

    async def update(self, run_id, **fields):
        if "progress" in fields:
            self.progress_writes += 1
        for k, v in fields.items():
            setattr(self.runs[run_id], k, v)

    async def finish(
        self,
        run_id,
        *,
        status,
        result,
        error,
        tokens_in,
        tokens_out,
        searches_used,
        assistant_text,
        table,
        attachments,
        model_used,
        task_type,
    ):
        rec = self.runs[run_id]
        if rec.status in TERMINAL:
            return None
        message_id = len(self.messages) + 1
        self.messages.append(
            {
                "session_id": rec.session_id,
                "role": "ai",
                "content": assistant_text,
                "table": table,
                "attachments": attachments,
                "model_used": model_used,
                "task_type": task_type,
                "id": message_id,
            }
        )
        rec.status, rec.error = status, error
        rec.result = None if result is None else {**result, "message_id": message_id}
        rec.tokens_in, rec.tokens_out, rec.searches_used = tokens_in, tokens_out, searches_used
        rec.finished_at = utcnow()
        return message_id

    async def log_request(self, **fields):
        self.logs.append(fields)

    async def mark_interrupted(self, message_for):
        n = 0
        for rec in self.runs.values():
            if rec.status in ACTIVE:
                rec.status = "failed"
                rec.error = {"code": "interrupted", "message": "перезапуск"}
                rec.finished_at = utcnow()
                if rec.session_id is not None:
                    self.messages.append(
                        {
                            "session_id": rec.session_id,
                            "role": "ai",
                            "content": message_for(rec.template_id),
                        }
                    )
                n += 1
        return n


ECHO_YAML = """
id: _echo
title: Эхо
description: тестовый шаблон
icon: file
allowed_roles: [pm, admin]
files:
  - {slot: doc, label: Документ, accept: [.docx], required: true, multiple: false}
  - {slot: extra, label: Дополнительно, accept: [.docx, .xlsx], required: false, multiple: true}
params:
  - {key: note, label: Заметка, type: text, required: false, max_length: 20}
  - {key: kind, label: Вид, type: select, options: [{value: a, label: Вариант А}]}
  - {key: count, label: Число, type: number, min: 1, max: 5}
require_any: []
output: table
pipeline: _echo
limits: {max_items: 5, max_searches_per_item: 2, concurrency: 2, run_timeout_s: 30, max_runs_per_user: 2}
models: {default: claude-sonnet}
"""


class Echo:
    """Управляемое поведение пайплайна _echo."""

    sleep = 0.0
    raises: Exception | None = None
    warnings: ClassVar[list[str]] = []

    @classmethod
    def reset(cls):
        cls.sleep, cls.raises, cls.warnings = 0.0, None, []


async def echo_pipeline(ctx: PipelineContext) -> PipelineResult:
    await ctx.progress("read_files", 0, 1, "Чтение")
    doc = await ctx.file("doc")
    for w in Echo.warnings:
        ctx.add_warning(w)
    await ctx.progress("split_items", 1, 3, "Работа")
    if Echo.sleep:
        await asyncio.sleep(Echo.sleep)
    if Echo.raises:
        raise Echo.raises
    rows = [{"№": 1, "Файл": doc.name, "Размер": len(doc.data)}]
    attachment = await ctx.upload_output("out.txt", b"hello", "text/plain")
    return PipelineResult(
        text="Готово", table=rows, attachments=[attachment], stats={"items_found": 1}
    )


def write_echo_template(root: Path, yaml_text: str = ECHO_YAML) -> Path:
    d = root / "_echo"
    d.mkdir(parents=True, exist_ok=True)
    (d / "template.yaml").write_text(yaml_text, encoding="utf-8")
    return root


def make_manager(tmp_path: Path, yaml_text: str = ECHO_YAML, **kw):
    register_pipeline("_echo", echo_pipeline)
    registry = load_registry(write_echo_template(tmp_path, yaml_text))
    store, storage = MemoryRunStore(), MemoryStorage()
    storage.put(
        USER_KEY,
        DOCX,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "ab12_input.docx",
    )
    adapters = fake_adapters()
    manager = RunManager(
        registry=registry,
        store=store,
        storage=storage,
        adapters_provider=lambda: adapters,
        progress_interval=0.0,
        **kw,
    )
    return manager, store, storage, adapters


def cleanup():
    unregister_pipeline("_echo")
    Echo.reset()
