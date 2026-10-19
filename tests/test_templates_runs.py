"""Этап 2: реестр шаблонов, исполнитель runs, эндпоинты, PipelineContext.
БД и R2 заменены памятью (tests/templates_helpers.py), провайдеры — FakeAdapter."""

import asyncio

import httpx
import pytest

from app.adapters.base import FINISH_MAX_TOKENS, SourceRef
from app.auth import CurrentUser, get_current_user
from app.errors import OutputTruncated
from app.main import app
from app.templates.errors import RunError
from app.templates.runner import get_run_manager, warnings_block
from app.templates.safety import wrap_document
from app.templates.spec import TemplateConfigError, load_registry
from tests.helpers import FakeAdapter, result
from tests.templates_helpers import (
    ECHO_YAML,
    OTHER_KEY,
    USER_KEY,
    Echo,
    cleanup,
    make_manager,
    write_echo_template,
)

PM = CurrentUser(user_id=12, role="pm")
ADMIN = CurrentUser(user_id=1, role="admin")
WAREHOUSE = CurrentUser(user_id=12, role="warehouse")
FILES = {"doc": [USER_KEY]}


@pytest.fixture
def env(tmp_path):
    manager, store, storage, adapters = make_manager(tmp_path)
    yield manager, store, storage, adapters
    cleanup()


@pytest.fixture
async def client(env):
    manager = env[0]
    state = {"user": PM}
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    app.dependency_overrides[get_run_manager] = lambda: manager
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        c.state = state
        yield c
    app.dependency_overrides.clear()


async def done(manager, run_id, user=PM):
    await manager.wait(run_id)
    return await manager.get_run(user, run_id)


# --- реестр ---------------------------------------------------------------------------------


def test_registry_loads_and_filters_by_role(tmp_path):
    from app.templates.pipelines import register_pipeline

    register_pipeline("_echo", lambda ctx: None)
    try:
        reg = load_registry(write_echo_template(tmp_path))
        assert [t.id for t in reg.for_role("pm")] == ["_echo"]
        assert reg.for_role("warehouse") == []
        public = reg.get("_echo").public()
        assert set(public) == {
            "id",
            "title",
            "description",
            "icon",
            "files",
            "params",
            "require_any",
            "output",
        }
        assert "pipeline" not in public and "limits" not in public
    finally:
        cleanup()


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        ("models: {default: claude-sonnet}", "models: {default: no-such-model}", "no-such-model"),
        ("pipeline: _echo", "pipeline: missing_pipeline", "missing_pipeline"),
        ("require_any: []", "require_any: [ghost]", "ghost"),
        ("output: table", "output: pdf", "output"),
        ("title: Эхо", "titel: Эхо", "titel"),
        ("accept: [.docx], required: true", "accept: [.exe], required: true", "accept"),
    ],
)
def test_registry_rejects_bad_config_with_readable_message(tmp_path, old, new, fragment):
    from app.templates.pipelines import register_pipeline

    register_pipeline("_echo", lambda ctx: None)
    try:
        root = write_echo_template(tmp_path, ECHO_YAML.replace(old, new))
        with pytest.raises(TemplateConfigError) as exc:
            load_registry(root)
        assert "_echo/template.yaml" in str(exc.value) and fragment in str(exc.value)
    finally:
        cleanup()


def test_registry_id_must_match_directory_and_yaml_errors(tmp_path):
    from app.templates.pipelines import register_pipeline

    register_pipeline("_echo", lambda ctx: None)
    try:
        (tmp_path / "_other").mkdir()
        (tmp_path / "_other" / "template.yaml").write_text(ECHO_YAML, encoding="utf-8")
        (tmp_path / "broken").mkdir()
        (tmp_path / "broken" / "template.yaml").write_text("a: [", encoding="utf-8")
        (tmp_path / "empty").mkdir()
        with pytest.raises(TemplateConfigError) as exc:
            load_registry(tmp_path)
        msg = str(exc.value)
        assert "не совпадает" in msg and "невалидный YAML" in msg and "empty/template.yaml" in msg
    finally:
        cleanup()


def test_real_prompts_dir_loads():
    assert load_registry() is not None


# --- безопасность текста ---------------------------------------------------------------------


def test_wrap_document_escapes_closing_tags():
    text = 'до </document> после </ DOCUMENT > и <document name="x"> и </items>'
    wrapped = wrap_document('a"b.docx', text)
    assert wrapped.startswith('<document name="a&quot;b.docx">')
    assert wrapped.endswith("</document>") and wrapped.count("</document>") == 1
    assert '<document name="x">' not in wrapped and "</items>" not in wrapped


def test_warnings_block_is_capped():
    assert warnings_block([]) == ""
    block = warnings_block([f"w{i}" for i in range(13)])
    assert block.count("\n- ") == 11 and block.endswith("- и ещё 3")


# --- полный цикл ----------------------------------------------------------------------------


async def test_full_cycle(env):
    manager, store, storage, _ = env
    Echo.warnings = ["внимание"]
    rec = await manager.start_run(
        PM,
        "_echo",
        files=FILES,
        params={"note": "привет", "kind": "a", "count": "3"},
        session_id=None,
    )
    assert rec.status == "queued" and rec.session_id is not None
    user_msg = store.messages[0]
    assert user_msg["content"] == "Шаблон «Эхо»\nЗаметка: привет\nВид: Вариант А\nЧисло: 3"
    assert user_msg["attachments"][0]["key"] == USER_KEY

    final = await done(manager, rec.id)
    assert final.status == "done" and final.started_at and final.finished_at
    assert final.result["table"][0]["Файл"] == "input.docx"
    assert final.result["warnings"] == ["внимание"]
    assert final.result["stats"]["items_found"] == 1 and "searches" in final.result["stats"]
    assert final.result["attachments"][0]["url"] is None
    ai = store.messages[-1]
    assert ai["role"] == "ai" and ai["table"] == final.result["table"]
    assert ai["content"].startswith("Готово") and "Предупреждения:\n- внимание" in ai["content"]
    assert ai["attachments"][0]["key"].startswith("ai/12/")
    assert final.result["message_id"] == ai["id"]
    assert final.progress["stage"] == "split_items"
    # выходной файл реально лежит в хранилище
    assert ai["attachments"][0]["key"] in storage.objects


async def test_same_session_is_reused_and_foreign_session_rejected(env):
    manager, _store, *_ = env
    first = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    await done(manager, first.id)
    second = await manager.start_run(
        PM, "_echo", files=FILES, params={}, session_id=first.session_id
    )
    assert second.session_id == first.session_id
    await done(manager, second.id)
    with pytest.raises(Exception) as exc:
        await manager.start_run(
            CurrentUser(user_id=99, role="pm"),
            "_echo",
            files={"doc": ["ai/99/_pending/uploads/x.docx"]},
            params={},
            session_id=first.session_id,
        )
    assert getattr(exc.value, "status_code", None) in (404,)


async def test_runtime_errors_become_failed(env):
    manager, store, *_ = env
    Echo.raises = RunError("too_many_items", "Найдено 70, максимум 60")
    rec = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    final = await done(manager, rec.id)
    assert final.status == "failed" and final.error["code"] == "too_many_items"
    assert final.result is None
    assert (
        store.messages[-1]["content"]
        == "Не удалось выполнить шаблон «Эхо»: Найдено 70, максимум 60"
    )


async def test_unexpected_exception_does_not_leak_details(env):
    manager, store, *_ = env
    Echo.raises = RuntimeError("Traceback sk-ant-SECRET key=abc")
    rec = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    final = await done(manager, rec.id)
    assert final.error["code"] == "generation_failed"
    assert "SECRET" not in str(final.error) and "SECRET" not in store.messages[-1]["content"]


async def test_output_truncated_maps_to_its_code(env):
    manager, *_ = env
    Echo.raises = OutputTruncated("max_tokens (out=1)")
    rec = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    assert (await done(manager, rec.id)).error["code"] == "output_truncated"


async def test_cancel_in_the_middle(env):
    manager, store, *_ = env
    Echo.sleep = 30
    rec = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    await asyncio.sleep(0.05)
    cancelled = await manager.cancel_run(PM, rec.id)
    assert cancelled.status == "cancelled" and cancelled.finished_at
    assert "отменён" in store.messages[-1]["content"]
    again = await manager.cancel_run(PM, rec.id)  # повтор — без ошибки, статус не меняется
    assert again.status == "cancelled"


async def test_cancel_of_finished_run_keeps_status(env):
    manager, *_ = env
    rec = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    await done(manager, rec.id)
    assert (await manager.cancel_run(PM, rec.id)).status == "done"


async def test_run_timeout(tmp_path):
    manager, _store, *_ = make_manager(
        tmp_path, ECHO_YAML.replace("run_timeout_s: 30", "run_timeout_s: 1")
    )
    try:
        Echo.sleep = 30
        rec = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
        final = await done(manager, rec.id)
        assert final.status == "failed" and final.error["code"] == "timeout"
    finally:
        cleanup()


async def test_recover_marks_active_runs_interrupted(env):
    manager, store, *_ = env
    Echo.sleep = 30
    rec = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    await asyncio.sleep(0.05)
    # «перезапуск»: новый процесс видит в БД незавершённый run
    for task in list(manager._tasks.values()):
        task.cancel()
    store.runs[rec.id].status = "running"
    assert await manager.recover() == 1
    assert (
        store.runs[rec.id].status == "failed" and store.runs[rec.id].error["code"] == "interrupted"
    )


async def test_recover_adds_history_message_for_each_interrupted_run(env):
    manager, store, *_ = env
    Echo.sleep = 30
    rec = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    await asyncio.sleep(0.05)
    for task in list(manager._tasks.values()):
        task.cancel()
    await asyncio.sleep(0.05)  # обработчики отмены отработали
    store.runs[rec.id].status = "running"
    done_rec = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    await asyncio.sleep(0.05)
    for task in list(manager._tasks.values()):
        task.cancel()
    await asyncio.sleep(0.05)
    store.runs[done_rec.id].status = "done"  # завершённый не трогаем
    before = len(store.messages)
    assert await manager.recover() == 1
    added = store.messages[before:]
    assert added == [
        {
            "session_id": rec.session_id,
            "role": "ai",
            "content": "Запуск шаблона «Эхо» прерван перезапуском сервиса.",
        }
    ]


async def test_lifespan_calls_recover_on_startup(env):
    manager, store, *_ = env
    Echo.sleep = 30
    rec = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    await asyncio.sleep(0.05)
    for task in list(manager._tasks.values()):
        task.cancel()
    await asyncio.sleep(0.05)
    store.runs[rec.id].status = "running"  # run «прошлого процесса»
    app.dependency_overrides[get_run_manager] = lambda: manager
    try:
        async with app.router.lifespan_context(app):
            assert store.runs[rec.id].status == "failed"
            assert store.runs[rec.id].error["code"] == "interrupted"
            assert store.messages[-1]["content"].startswith("Запуск шаблона «Эхо» прерван")
    finally:
        app.dependency_overrides.clear()


async def test_lifespan_survives_unavailable_store(env):
    manager, store, *_ = env

    async def boom(_message_for):
        raise ConnectionError("db down")

    store.mark_interrupted = boom
    app.dependency_overrides[get_run_manager] = lambda: manager
    try:
        async with app.router.lifespan_context(app):
            pass  # старт не падает
    finally:
        app.dependency_overrides.clear()


async def test_run_limit_per_user(env):
    manager, *_ = env
    Echo.sleep = 30
    r1 = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    r2 = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    with pytest.raises(Exception) as exc:
        await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    assert exc.value.status_code == 429 and exc.value.detail["code"] == "run_limit_exceeded"
    await manager.cancel_run(PM, r1.id)  # освободили слот — снова можно
    r3 = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    for r in (r2, r3):
        await manager.cancel_run(PM, r.id)


# --- эндпоинты ------------------------------------------------------------------------------


async def test_http_list_templates_by_role(client):
    body = (await client.get("/v1/templates")).json()
    assert [t["id"] for t in body] == ["_echo"] and body[0]["files"][0]["slot"] == "doc"
    client.state["user"] = CurrentUser(user_id=5, role="warehouse")
    assert (await client.get("/v1/templates")).json() == []


async def test_http_create_poll_and_errors(client, env):
    manager = env[0]
    r = await client.post(
        "/v1/templates/_echo/runs", json={"session_id": None, "files": FILES, "params": {}}
    )
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"run_id", "session_id", "status"} and body["status"] == "queued"
    await manager.wait(body["run_id"])
    got = (await client.get(f"/v1/runs/{body['run_id']}")).json()
    assert got["status"] == "done" and got["error"] is None and got["result"]["table"]
    assert set(got) >= {
        "run_id",
        "template_id",
        "session_id",
        "status",
        "progress",
        "result",
        "error",
        "created_at",
        "started_at",
        "finished_at",
    }


@pytest.mark.parametrize(
    ("template", "payload", "status", "code", "field"),
    [
        ("nope", {"files": FILES}, 404, "template_not_found", None),
        ("_echo", {"files": {}}, 422, "template_input_invalid", "doc"),
        (
            "_echo",
            {"files": {"doc": [USER_KEY, USER_KEY.replace("ab12", "ab13")]}},
            422,
            "template_input_invalid",
            "doc",
        ),
        (
            "_echo",
            {"files": {"doc": ["ai/12/_pending/uploads/ab12_x.pdf"]}},
            422,
            "template_input_invalid",
            "doc",
        ),
        (
            "_echo",
            {"files": FILES, "params": {"note": "x" * 21}},
            422,
            "template_input_invalid",
            "note",
        ),
        (
            "_echo",
            {"files": FILES, "params": {"kind": "zzz"}},
            422,
            "template_input_invalid",
            "kind",
        ),
        (
            "_echo",
            {"files": FILES, "params": {"count": 99}},
            422,
            "template_input_invalid",
            "count",
        ),
        ("_echo", {"files": FILES, "params": {"ghost": 1}}, 422, "template_input_invalid", "ghost"),
        ("_echo", {"files": {"doc": [OTHER_KEY]}}, 403, "ai_file_forbidden", "doc"),
        (
            "_echo",
            {"files": {"doc": ["ai/12/_pending/uploads/ab12_missing.docx"]}},
            404,
            "file_not_found",
            "doc",
        ),
        ("_echo", {"files": {"doc": ["../../etc/passwd.docx"]}}, 403, "ai_file_forbidden", "doc"),
    ],
)
async def test_http_input_validation(client, template, payload, status, code, field):
    r = await client.post(f"/v1/templates/{template}/runs", json=payload)
    assert r.status_code == status
    detail = r.json()["detail"]
    assert detail["code"] == code and detail.get("field") == field and detail["message"]


async def test_http_role_forbidden(client):
    client.state["user"] = WAREHOUSE
    r = await client.post("/v1/templates/_echo/runs", json={"files": FILES})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "template_forbidden"


async def test_http_require_any(tmp_path):
    manager, *_ = make_manager(
        tmp_path,
        ECHO_YAML.replace("require_any: []", "require_any: [note, extra]").replace(
            "{slot: doc, label: Документ, accept: [.docx], required: true",
            "{slot: doc, label: Документ, accept: [.docx], required: false",
        ),
    )
    app.dependency_overrides[get_current_user] = lambda: PM
    app.dependency_overrides[get_run_manager] = lambda: manager
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            r = await c.post("/v1/templates/_echo/runs", json={"files": {}, "params": {}})
            assert r.status_code == 422 and "хотя бы одно" in r.json()["detail"]["message"]
    finally:
        app.dependency_overrides.clear()
        cleanup()


async def test_http_foreign_run_is_404_admin_can_read(client, env):
    manager = env[0]
    rec = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    await manager.wait(rec.id)
    client.state["user"] = CurrentUser(user_id=99, role="pm")
    for path in (f"/v1/runs/{rec.id}", "/v1/runs/not-a-uuid", f"/v1/runs/{rec.id[:-1]}0"):
        r = await client.get(path)
        assert r.status_code == 404 and r.json()["detail"]["code"] == "run_not_found"
    r = await client.post(f"/v1/runs/{rec.id}/cancel")
    assert r.status_code == 404
    client.state["user"] = ADMIN
    assert (await client.get(f"/v1/runs/{rec.id}")).json()["status"] == "done"


async def test_http_limit_and_cancel(client, env):
    Echo.sleep = 30
    ids = []
    for _ in range(2):
        r = await client.post("/v1/templates/_echo/runs", json={"files": FILES})
        assert r.status_code == 202
        ids.append(r.json()["run_id"])
    r = await client.post("/v1/templates/_echo/runs", json={"files": FILES})
    assert r.status_code == 429 and r.json()["detail"]["code"] == "run_limit_exceeded"
    c = await client.post(f"/v1/runs/{ids[0]}/cancel")
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    await client.post(f"/v1/runs/{ids[1]}/cancel")


async def test_http_bad_session_id(client):
    r = await client.post("/v1/templates/_echo/runs", json={"files": FILES, "session_id": "abc"})
    assert r.status_code == 422 and r.json()["detail"]["field"] == "session_id"


# --- PipelineContext ------------------------------------------------------------------------


async def make_ctx(env, **overrides):
    manager, store, storage, adapters = env
    rec = await manager.start_run(PM, "_echo", files=FILES, params={}, session_id=None)
    await manager.wait(rec.id)
    from app.templates.context import PipelineContext

    async def write_progress(p):
        await store.update(rec.id, progress=p)

    spec = manager.registry.get("_echo")
    ctx = PipelineContext(
        run_id=rec.id,
        spec=spec,
        user=PM,
        session_id=rec.session_id,
        params={},
        input_files=rec.input_files,
        adapters=adapters,
        storage=storage,
        write_progress=write_progress,
        log_request=store.log_request,
        global_semaphore=asyncio.Semaphore(8),
        **overrides,
    )
    return ctx, store, adapters


async def test_ctx_generate_applies_strict_finish_and_logs(env):
    ctx, store, adapters = await make_ctx(env)
    adapters["claude-sonnet"].script = [
        result(text="{", finish=FINISH_MAX_TOKENS, tokens_in=7, tokens_out=3)
    ]
    with pytest.raises(OutputTruncated):
        await ctx.generate(system="S", user="U")
    # токены учтены даже при обрыве, запись в лог — со статусом error и привязкой к run
    assert (ctx.tokens_in, ctx.tokens_out) == (7, 3)
    log = store.logs[-1]
    assert (
        log["status"] == "error" and log["template_id"] == "_echo" and log["run_id"] == ctx.run_id
    )
    assert log["task_type"] == "template:_echo" and "OutputTruncated" in log["error_message"]


async def test_ctx_generate_accumulates_and_logs_success(env):
    ctx, store, adapters = await make_ctx(env)
    first = result(text="a", tokens_in=10, tokens_out=5, model_used="claude-sonnet-5-5")
    first.sources, first.searches_used = [SourceRef("https://a.example", "A")], 2
    second = result(text="b", tokens_in=1, tokens_out=1)
    second.sources, second.searches_used = (
        [SourceRef("https://a.example", "A"), SourceRef("https://b.example")],
        1,
    )
    adapter: FakeAdapter = adapters["claude-sonnet"]
    adapter.script = [first, second]
    await ctx.generate(system="S", user="U1", web_search=True, web_search_max_uses=3)
    await ctx.generate(system="S", user="U2", web_search=True, web_search_max_uses=3)
    assert (ctx.tokens_in, ctx.tokens_out, ctx.searches_used) == (11, 6, 3)
    assert [s.url for s in ctx.sources] == ["https://a.example", "https://b.example"]
    assert [entry["searches_used"] for entry in store.logs] == [2, 1]
    assert all(e["status"] == "success" and e["run_id"] == ctx.run_id for e in store.logs)
    call = adapter.calls[0]
    assert call["web_search"] is True and call["web_search_max_uses"] == 3
    assert call["tools"] == [] and call["system"] == "S"
    assert ctx.warnings == []


async def test_ctx_generate_warns_when_search_limit_exceeded_but_does_not_fail(env):
    ctx, _, adapters = await make_ctx(env)
    over = result(text="ok")
    over.searches_used = 5
    adapters["claude-sonnet"].script = [over]
    res = await ctx.generate(system="S", user="U", web_search=True, web_search_max_uses=2)
    assert res.text == "ok" and any("5 из 2" in w for w in ctx.warnings)


async def test_ctx_generate_without_search_does_not_pass_limit(env):
    ctx, _, adapters = await make_ctx(env)
    await ctx.generate(system="S", user="U", web_search_max_uses=3)
    assert adapters["claude-sonnet"].calls[0]["web_search_max_uses"] is None


async def test_ctx_generate_respects_local_concurrency(env):
    ctx, _, _adapters = await make_ctx(env)
    state = {"now": 0, "max": 0}

    class Slow(FakeAdapter):
        async def generate(self, prompt=None, **kwargs):
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            await asyncio.sleep(0.02)
            state["now"] -= 1
            return result(text="x")

    ctx._adapters = {"claude-sonnet": Slow("claude-sonnet")}
    await asyncio.gather(*(ctx.generate(system="S", user="U") for _ in range(6)))
    assert state["max"] == 2  # limits.concurrency из template.yaml


async def test_ctx_progress_is_throttled_and_flushed(env):
    ctx, store, _ = await make_ctx(env, progress_interval=60.0)
    before = store.progress_writes
    for i in range(20):
        await ctx.progress("search_brands", i, 20, f"{i}")
    assert store.progress_writes == before + 1
    await ctx.flush_progress()
    assert store.progress_writes == before + 2
    assert store.runs[ctx.run_id].progress["done"] == 19


async def test_ctx_reads_files_lazily_and_checks_magic(env):
    ctx, _, _ = await make_ctx(env)
    storage = env[2]
    files = await ctx.files("doc")
    assert files[0].kind == "docx" and files[0].name == "input.docx"
    assert await ctx.file("extra") is None
    bad_key = "ai/12/_pending/uploads/ab12_bad.docx"
    storage.put(bad_key, b"PK\x03\x04 not a docx", "application/octet-stream", "ab12_bad.docx")
    ctx._input_files = {"doc": [{"name": "bad.docx", "key": bad_key, "mime": "x", "size": 1}]}
    ctx._file_cache.clear()
    with pytest.raises(RunError) as exc:
        await ctx.files("doc")
    assert exc.value.code == "generation_failed" and "bad.docx" in exc.value.message
