from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.auth import CurrentUser, get_current_user
from app.db.session import get_db
from app.main import app
from app.utils import attachments as att_mod
from app.utils.attachments import describe_kinds, kinds_of_records, store_user_upload
from app.utils.storage import ensure_extension
from tests.helpers import FakeDB

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.mark.parametrize(
    ("name", "mime", "expected"),
    [
        ("clipboard", "image/png", "clipboard.png"),
        ("clipboard", "image/jpeg", "clipboard.jpg"),
        ("image", "image/webp", "image.webp"),
        ("scan", "application/pdf", "scan.pdf"),
        ("Договор", DOCX, "Договор.docx"),
        ("Смета", XLSX, "Смета.xlsx"),
        ("photo.PNG", "image/png", "photo.PNG"),  # расширение уже есть — не трогаем
        ("report.pdf", "application/pdf", "report.pdf"),
        ("blob", "text/plain", "blob"),  # неизвестный mime
    ],
)
def test_extension_from_mime(name, mime, expected):
    assert ensure_extension(name, mime) == expected


async def test_store_user_upload_adds_extension_to_name_and_key(monkeypatch):
    saved = []

    async def fake_put(key, data, content_type, *, name=None):
        saved.append((key, name))

    monkeypatch.setattr(att_mod, "put_bytes_async", fake_put)
    rec = await store_user_upload(7, 42, "clipboard", b"x", "image/png")
    assert rec["name"] == "clipboard.png" and rec["type"] == "image"
    assert saved[0][0].startswith("ai/7/42/uploads/") and saved[0][0].endswith("_clipboard.png")
    assert saved[0][1] == "clipboard.png"
    rec = await store_user_upload(7, 42, "", b"x", "image/jpeg")
    assert rec["name"] == "attachment.jpg"


def test_describe_kinds_for_classifier():
    assert describe_kinds([]) == ""
    assert describe_kinds(["pdf", "image", "image"]) == "attached: 1 pdf, 2 images"
    assert describe_kinds(["image"]) == "attached: 1 image"
    records = [
        {"type": "image", "mime": "image/png"},
        {"type": "file", "mime": "application/pdf"},
        {"type": "file", "mime": DOCX},
        {"type": "file", "mime": XLSX},
        {"type": "file", "mime": "application/zip"},
    ]
    assert kinds_of_records(records) == ["image", "pdf", "docx", "xlsx", "file"]


def test_history_endpoint_returns_image_model_used_next_to_model_used():
    msgs = [
        SimpleNamespace(
            role="ai",
            content="🎨 Готово: кот",
            model_used="claude-sonnet-5-5",
            task_type="image",
            created_at=datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
            table_data=None,
            attachments=[],
            image_model="gpt-image-2.5-sunburst",
        ),
        SimpleNamespace(
            role="user",
            content="нарисуй",
            model_used=None,
            task_type=None,
            created_at=datetime(2026, 10, 5, 11, 59, tzinfo=UTC),
            table_data=None,
            attachments=[],
            image_model=None,
        ),
    ]
    db = FakeDB(existing_session=SimpleNamespace(id=5, user_id=7))
    db.history_rows = list(reversed(msgs))  # FakeDB.execute отдаёт в обратном порядке
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id=7, role="pm")
    try:
        data = TestClient(app).get("/v1/sessions/5/messages").json()
    finally:
        app.dependency_overrides.clear()
    ai = next(m for m in data if m["role"] == "ai")
    user = next(m for m in data if m["role"] == "user")
    assert (
        ai["model_used"] == "claude-sonnet-5-5"
        and ai["image_model_used"] == "gpt-image-2.5-sunburst"
    )
    assert user["image_model_used"] is None
    assert set(ai) == {
        "role",
        "content",
        "model_used",
        "task_type",
        "created_at",
        "table",
        "attachments",
        "image_model_used",
    }
