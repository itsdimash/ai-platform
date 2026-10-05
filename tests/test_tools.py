import pytest

from app import tools
from app.adapters.base import GenerationResult
from app.tools import ToolContext, ToolExecutionError, apply_tool_calls

CTX = ToolContext(user_id=7, session_id=42)


@pytest.fixture
def stored(monkeypatch):
    saved: list[dict] = []

    async def fake_put(key, data, content_type, *, name=None):
        saved.append({"key": key, "size": len(data), "mime": content_type, "name": name})

    monkeypatch.setattr(tools, "put_bytes_async", fake_put)
    return saved


def _result(calls, text="Сейчас создам..."):
    return GenerationResult(text=text, tokens_in=1, tokens_out=1, latency_ms=1, tool_calls=calls)


async def test_presentation_becomes_attachment_with_summary(stored):
    res = await apply_tool_calls(
        _result(
            [
                {
                    "name": "generate_presentation",
                    "args": {"title": "План", "slides": [{"title": "A", "content": ["x"]}] * 4},
                }
            ]
        ),
        ctx=CTX,
        prompt="p",
    )
    assert len(res.attachments) == 1
    att = res.attachments[0]
    assert att["type"] == "file" and att["name"] == "План.pptx" and att["size"] > 0
    assert att["key"].startswith("ai/7/42/") and att["key"].endswith("_План.pptx")
    assert "url" not in att  # url в записи не хранится
    assert "Презентация «План» — 5 слайдов" in res.text  # 4 + титульный
    assert "Сейчас создам" not in res.text  # текст модели заменён резюме
    assert stored[0]["key"] == att["key"]


@pytest.mark.parametrize(
    ("name", "args", "ext", "kind"),
    [
        (
            "generate_document",
            {"title": "Док", "sections": [{"paragraphs": ["a"]}]},
            ".docx",
            "file",
        ),
        (
            "generate_spreadsheet",
            {"filename": "Т", "sheets": [{"name": "s", "headers": ["a"], "rows": [["1"]]}]},
            ".xlsx",
            "file",
        ),
        ("generate_pdf", {"title": "П", "sections": [{"paragraphs": ["a"]}]}, ".pdf", "file"),
    ],
)
async def test_other_file_tools(stored, name, args, ext, kind):
    res = await apply_tool_calls(_result([{"name": name, "args": args}]), ctx=CTX, prompt="p")
    assert res.attachments[0]["name"].endswith(ext)
    assert res.attachments[0]["type"] == kind


async def test_image_tool_is_image_attachment(stored, monkeypatch):
    async def fake_image(prompt, size):
        return b"\x89PNG\r\n\x1a\nfake"

    monkeypatch.setattr(tools, "generate_image_bytes", fake_image)
    res = await apply_tool_calls(
        _result([{"name": "generate_image", "args": {"prompt": "cat"}}]), ctx=CTX, prompt="p"
    )
    assert res.attachments[0]["type"] == "image" and res.attachments[0]["mime"] == "image/png"


async def test_no_tool_calls_leaves_result_untouched(stored):
    res = await apply_tool_calls(_result([], text="Обычный ответ"), ctx=CTX, prompt="p")
    assert res.text == "Обычный ответ" and res.attachments == [] and stored == []


async def test_unknown_tool_is_ignored(stored):
    res = await apply_tool_calls(
        _result([{"name": "web_search", "args": {}}], text="ответ"), ctx=CTX, prompt="p"
    )
    assert res.text == "ответ" and res.attachments == []


async def test_builder_failure_raises_tool_execution_error(stored, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("builder exploded")

    monkeypatch.setattr(tools, "build_presentation", boom)
    with pytest.raises(ToolExecutionError) as exc_info:
        await apply_tool_calls(
            _result([{"name": "generate_presentation", "args": {"title": "x", "slides": []}}]),
            ctx=CTX,
            prompt="p",
        )
    assert exc_info.value.tool_name == "generate_presentation"
    assert stored == []


async def test_storage_failure_raises_tool_execution_error(monkeypatch):
    async def fail_put(*a, **k):
        raise OSError("r2 down")

    monkeypatch.setattr(tools, "put_bytes_async", fail_put)
    with pytest.raises(ToolExecutionError):
        await apply_tool_calls(
            _result([{"name": "generate_document", "args": {"title": "x", "sections": []}}]),
            ctx=CTX,
            prompt="p",
        )
