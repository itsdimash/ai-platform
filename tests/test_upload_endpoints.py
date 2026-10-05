from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import limits
from app.adapters.anthropic_adapter import AnthropicAdapter
from app.adapters.base import GenerationResult
from app.adapters.gemini_adapter import GeminiAdapter
from app.adapters.openai_adapter import OpenAIAdapter
from app.auth import CurrentUser, get_current_user
from app.classifier.classify import Classification
from app.db.session import get_db
from app.main import app
from app.models.chat import ChatMessage, ChatSession
from app.models.logs import AIRequestLog
from app.routers import chat_multimodal, document_extract
from app.routers.deps import get_adapters
from app.utils import attachments as attachments_mod
from app.utils import uploads
from app.utils.docx_builder import build_document
from app.utils.pdf_builder import build_pdf
from app.utils.storage import make_record
from tests.test_media import noisy_png

DOCX = build_document("Договор поставки", [{"paragraphs": ["Предмет договора: болты"]}])
PDF = build_pdf("Счёт", [{"paragraphs": ["Итого к оплате 100"]}])
PNG = noisy_png(32, 32)


class FakeResult:
    def scalars(self):
        return self

    def all(self):
        return []


class FakeDB:
    def __init__(self, existing_session=None):
        self.added: list = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self._existing = existing_session

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if isinstance(obj, ChatSession) and obj.id is None:
                obj.id = 99

    async def execute(self, stmt):
        return FakeResult()

    async def get(self, model, pk):
        return self._existing

    def of(self, cls):
        return [o for o in self.added if isinstance(o, cls)]


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    adapters = {
        "claude-sonnet": AnthropicAdapter(api_key="x", model="claude-sonnet-5"),
        "claude-haiku": AnthropicAdapter(api_key="x", model="claude-haiku-4-5-20251001"),
        "gemini-flash": GeminiAdapter(api_key="x", model="gemini-3.6-flash"),
        "gpt-4o": OpenAIAdapter(api_key="x", model="gpt-4o"),
    }
    captured: dict = {}
    stored: list[dict] = []

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return GenerationResult(text="ок", tokens_in=1, tokens_out=1, latency_ms=1)

    for adapter in adapters.values():
        monkeypatch.setattr(adapter, "generate", fake_generate)

    async def fake_store(user_id, session_id, filename, data, mime):
        record = make_record(
            name=filename,
            key=f"ai/{user_id}/{session_id}/uploads/k_{filename}",
            mime=mime,
            size=len(data),
        )
        stored.append({**record, "data": data})
        return record

    async def fake_classify(prompt, adapter):
        return Classification(task_type="general_qa", confidence=0.9, reasoning="")

    monkeypatch.setattr(chat_multimodal, "store_user_upload", fake_store)
    monkeypatch.setattr(document_extract, "store_user_upload", fake_store)
    monkeypatch.setattr(chat_multimodal, "classify", fake_classify)
    monkeypatch.setattr(
        document_extract, "with_urls", lambda recs: [{**r, "url": "https://signed/x"} for r in recs]
    )

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_adapters] = lambda: adapters
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id=7, role="pm")
    yield SimpleNamespace(client=TestClient(app), db=db, captured=captured, stored=stored)
    app.dependency_overrides.clear()


def mm(env, files, **data):
    payload = {"prompt": "проанализируй", **data}
    return env.client.post("/v1/chat/multimodal", data=payload, files=files)


def f(name, data, ctype="application/octet-stream"):
    return ("files", (name, data, ctype))


# --- /v1/chat/multimodal -------------------------------------------------------


def test_claude_gets_image_and_native_pdf_docx_goes_to_prompt_originals_stored(env):
    r = mm(env, [f("p.png", PNG), f("s.pdf", PDF), f("d.docx", DOCX)], model="claude-sonnet")
    assert r.status_code == 200, r.text
    kinds = sorted(a.kind for a in env.captured["attachments"])
    assert kinds == ["image", "pdf_document"]
    assert "Содержимое DOCX «d.docx»" in env.captured["prompt"]
    assert "Предмет договора: болты" in env.captured["prompt"]
    assert sorted(s["name"] for s in env.stored) == ["d.docx", "p.png", "s.pdf"]
    user_msg = next(m for m in env.db.of(ChatMessage) if m.role == "user")
    assert len(user_msg.attachments) == 3
    assert {a["type"] for a in user_msg.attachments} == {"image", "file"}


def test_octet_stream_content_type_is_fine_with_valid_extension(env):
    assert (
        mm(env, [f("p.png", PNG, "application/octet-stream")], model="claude-sonnet").status_code
        == 200
    )
    assert mm(env, [f("p.png", PNG, "")], model="claude-sonnet").status_code == 200


def test_openai_gets_pdf_as_text_not_native(env):
    r = mm(env, [f("s.pdf", PDF)], model="gpt-4o")
    assert r.status_code == 200, r.text
    assert not env.captured.get("attachments")
    assert "Содержимое PDF «s.pdf»" in env.captured["prompt"]
    assert "Итого к оплате 100" in env.captured["prompt"]


def test_haiku_pdf_over_100_pages_goes_to_text(env, monkeypatch):
    monkeypatch.setattr(chat_multimodal, "_safe_page_count", lambda data: 101)
    r = mm(env, [f("s.pdf", PDF)], model="claude-haiku")
    assert r.status_code == 200, r.text
    assert not env.captured.get("attachments")
    assert "слишком велик для нативной передачи" in env.captured["prompt"]
    # Sonnet с тем же PDF — нативно (лимит 600 страниц)
    r = mm(env, [f("s.pdf", PDF)], model="claude-sonnet")
    assert [a.kind for a in env.captured["attachments"]] == ["pdf_document"]


def test_oversized_image_downscaled_for_provider_but_original_saved(env, monkeypatch):
    monkeypatch.setattr(limits, "ANTHROPIC_IMAGE_MAX_RAW_BYTES", 12_000)
    monkeypatch.setattr(limits, "IMAGE_SHRINK_LADDER", (200, 100, 50))
    big = noisy_png(300, 300)
    assert len(big) > 12_000
    r = mm(env, [f("big.png", big)], model="claude-sonnet")
    assert r.status_code == 200, r.text
    sent = env.captured["attachments"][0]
    assert sent.mime_type == "image/jpeg" and len(sent.data) <= 12_000
    assert env.stored[0]["data"] == big  # в R2 — оригинал
    log = env.db.of(AIRequestLog)[0]
    assert "уменьшено" in (log.error_message or "")


def test_docx_only_request_uses_classifier_path(env):
    r = mm(env, [f("d.docx", DOCX)])
    assert r.status_code == 200, r.text
    assert r.json()["task_type"] == "general_qa"
    assert "Содержимое DOCX" in env.captured["prompt"]


def test_unsupported_and_mismatched_files_are_400_and_create_nothing(env):
    for name, data in (
        ("a.gif", b"GIF89a" + b"\x00" * 50),
        ("a.heic", b"\x00" * 50),
        ("a.png", PDF),
    ):
        r = mm(env, [f(name, data)], model="claude-sonnet")
        assert r.status_code == 400, (name, r.text)
    assert env.db.added == [] and env.stored == []


def test_more_than_10_files_is_400(env):
    r = mm(env, [f(f"{i}.png", PNG) for i in range(11)], model="claude-sonnet")
    assert r.status_code == 400 and "10" in r.json()["detail"]
    assert (
        mm(env, [f(f"{i}.png", PNG) for i in range(10)], model="claude-sonnet").status_code == 200
    )


def test_oversized_image_file_rejected_while_reading(env, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_IMAGE_BYTES", int(1.5 * limits.CHUNK_SIZE))
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (3 * limits.CHUNK_SIZE)
    r = mm(env, [f("huge.png", big)], model="claude-sonnet")
    assert r.status_code == 400 and "больше" in r.json()["detail"]


def test_total_size_limit_rejected(env, monkeypatch):
    monkeypatch.setattr(chat_multimodal, "MAX_TOTAL_UPLOAD_BYTES", 2 * limits.CHUNK_SIZE)
    chunk = PDF + b"\x00" * (limits.CHUNK_SIZE + 10)
    r = mm(env, [f("a.pdf", chunk), f("b.pdf", chunk)], model="claude-sonnet")
    assert r.status_code == 400 and "Суммарный" in r.json()["detail"]


def test_r2_failure_on_user_uploads_keeps_the_paid_answer(env, monkeypatch):
    async def broken(*a, **k):
        raise OSError("r2 down")

    monkeypatch.setattr(chat_multimodal, "store_user_upload", broken)
    r = mm(env, [f("p.png", PNG)], model="claude-sonnet")
    assert r.status_code == 200 and r.json()["text"] == "ок"
    user_msg = next(m for m in env.db.of(ChatMessage) if m.role == "user")
    assert user_msg.attachments == []
    assert "Не сохранены вложения" in env.db.of(AIRequestLog)[0].error_message


# --- /v1/documents/extract -------------------------------------------------------


def extract(env, name, data, ctype="application/octet-stream", **form):
    return env.client.post("/v1/documents/extract", files={"file": (name, data, ctype)}, data=form)


def test_extract_docx_returns_text_key_and_presigned_url(env):
    r = extract(env, "Договор.docx", DOCX, "")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "Предмет договора: болты" in body["text"]
    assert body["file_key"].startswith("ai/7/") and body["file_url"] == "https://signed/x"
    assert body["attachment"]["key"] == body["file_key"] and body["attachment"]["type"] == "file"
    assert body["truncated"] is False


def test_extract_pdf_and_rejections(env):
    assert extract(env, "s.pdf", PDF).status_code == 200
    for name, data in (("a.png", PNG), ("a.gif", b"GIF89a"), ("a.docx", PDF), ("a.pdf", DOCX)):
        r = extract(env, name, data)
        assert r.status_code == 400, (name, r.text)
    assert len(env.stored) == 1  # ничего лишнего в R2 не попало


def test_extract_unreadable_file_leaves_no_object(env):
    broken = b"PK\x03\x04" + b"\x00" * 100  # zip-подпись есть, но не docx
    assert extract(env, "a.docx", broken).status_code == 400
    assert env.stored == []


def test_extract_foreign_session_is_404(env):
    env.db._existing = SimpleNamespace(user_id=99)
    assert extract(env, "s.pdf", PDF, session_id="5").status_code == 404
    env.db._existing = SimpleNamespace(user_id=7)
    r = extract(env, "s.pdf", PDF, session_id="5")
    assert r.status_code == 200 and "/5/uploads/" in r.json()["file_key"]


def test_extract_over_50mb_limit_rejected_while_reading(env, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_DOCUMENT_BYTES", 2 * limits.CHUNK_SIZE)
    big = b"%PDF-" + b"\x00" * (4 * limits.CHUNK_SIZE)
    r = extract(env, "big.pdf", big)
    assert r.status_code == 400 and "больше" in r.json()["detail"]


# --- attachment_keys: только привязка, порядок, дубли, имена -------------------------

KEY_A = "ai/7/_pending/uploads/" + "a" * 32 + "_договор.docx"
KEY_B = "ai/7/_pending/uploads/" + "b" * 32 + "_смета.xlsx"


@pytest.fixture
def heads(monkeypatch):
    calls: list[str] = []

    async def fake_head(key):
        calls.append(key)
        return {"size": 10, "mime": "application/octet-stream", "name": None}

    monkeypatch.setattr(attachments_mod, "head_info_async", fake_head)
    return calls


def user_attachments(env):
    return next(m for m in env.db.of(ChatMessage) if m.role == "user").attachments


def test_keys_only_bind_no_text_goes_to_prompt(env, heads):
    r = mm(env, [], model="claude-sonnet", attachment_keys=[KEY_A])
    assert r.status_code == 200, r.text
    assert "договор" not in env.captured["prompt"]  # содержимое/имя ключа в промпт не попадает
    assert not env.captured.get("attachments")  # модели вложения по ключам не передаются
    assert [a["key"] for a in user_attachments(env)] == [KEY_A]
    assert heads == [KEY_A]  # только HEAD, один раз


def test_keys_come_first_then_uploaded_files_and_duplicate_keys_collapse(env, heads):
    r = mm(
        env,
        [f("x.png", PNG), f("y.pdf", PDF)],
        model="claude-sonnet",
        attachment_keys=[KEY_B, KEY_A, KEY_B],
    )
    assert r.status_code == 200, r.text
    names = [a["name"] for a in user_attachments(env)]
    keys = [a["key"] for a in user_attachments(env)]
    assert keys[:2] == [KEY_B, KEY_A]  # порядок первого появления, дубль схлопнут
    assert names[2:] == ["x.png", "y.pdf"]  # затем файлы в порядке загрузки
    assert len(keys) == 4 and sorted(heads) == sorted([KEY_A, KEY_B])


def test_more_than_10_keys_is_400_not_silently_truncated(env, heads):
    keys = [f"ai/7/_pending/uploads/{i:032x}_f.pdf" for i in range(11)]
    r = mm(env, [], model="claude-sonnet", attachment_keys=keys)
    assert r.status_code == 400 and "10" in r.json()["detail"]
    assert heads == [] and env.db.added == []


def test_foreign_or_missing_key_fails_before_any_model_call(env, heads, monkeypatch):
    r = mm(
        env,
        [],
        model="claude-sonnet",
        attachment_keys=["ai/8/_pending/uploads/" + "c" * 32 + "_x.pdf"],
    )
    assert r.status_code == 403 and "prompt" not in env.captured

    async def missing(key):
        return None

    monkeypatch.setattr(attachments_mod, "head_info_async", missing)
    r = mm(env, [], model="claude-sonnet", attachment_keys=[KEY_A])
    assert r.status_code == 404 and "prompt" not in env.captured


def test_json_chat_keys_dedupe_and_limit(env, heads, monkeypatch):
    async def fake_generate(**kwargs):
        env.captured.update(kwargs)
        return GenerationResult(text="ок", tokens_in=1, tokens_out=1, latency_ms=1)

    from app.routers import chat as chat_router

    async def fake_classify(prompt, adapter):
        return Classification(task_type="general_qa", confidence=0.9, reasoning="")

    monkeypatch.setattr(chat_router, "classify", fake_classify)
    for adapter in app.dependency_overrides[get_adapters]().values():
        monkeypatch.setattr(adapter, "generate", fake_generate)

    r = env.client.post(
        "/v1/chat",
        json={"prompt": "hi", "model": "claude-sonnet", "attachment_keys": [KEY_A, KEY_A]},
    )
    assert r.status_code == 200, r.text
    assert [a["key"] for a in user_attachments(env)] == [KEY_A]

    r = env.client.post("/v1/chat", json={"prompt": "hi", "attachment_keys": [KEY_A] * 11})
    assert r.status_code == 422


# --- имя вложения (name) ------------------------------------------------------------


def test_attachment_name_is_sanitized_original_name_in_both_endpoints(env):
    r = extract(env, "Отчёт Q3.PDF", PDF)
    assert r.status_code == 200, r.text
    assert r.json()["attachment"]["name"] == "Отчёт Q3.pdf"  # расширение — в нижнем регистре

    r = mm(env, [f("dir/Скан 1.PNG", PNG)], model="claude-sonnet")
    assert r.status_code == 200, r.text
    assert user_attachments(env)[0]["name"] == "Скан 1.png"  # путь отброшен, пробелы сохранены


def test_attachment_without_filename_extension_keeps_bare_name(env):
    r = mm(env, [f("clipboard", PNG)], model="claude-sonnet")
    assert r.status_code == 200, r.text
    att = user_attachments(env)[0]
    assert att["name"] == "clipboard" and att["mime"] == "image/png"
