from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import CurrentUser, get_current_user
from app.db.session import get_db
from app.main import app
from app.utils import attachments as att_mod
from app.utils.attachments import resolve_attachment_keys, with_urls

OWN = "ai/7/42/0123456789abcdef0123456789abcdef_f.pdf"


class FakeDB:
    def __init__(self, session):
        self._session = session
        self.execute = AsyncMock()
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    async def get(self, model, pk):
        return self._session


@pytest.fixture
def api():
    def make(session, user_id=7):
        db = FakeDB(session)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id=user_id, role="pm")
        return TestClient(app), db

    yield make
    app.dependency_overrides.clear()


def _session(user_id=7, title="Старое"):
    return SimpleNamespace(
        id=5, user_id=user_id, title=title, updated_at=datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    )


def test_rename_own_session(api):
    c, db = api(_session())
    r = c.patch("/v1/sessions/5", json={"title": "  Новое имя  "}, headers={"Authorization": "x"})
    assert r.status_code == 200 and r.json()["id"] == 5
    db.commit.assert_awaited_once()
    stmt = db.execute.await_args.args[0]
    compiled = str(stmt.compile())
    assert "title" in compiled
    # updated_at при переименовании остаётся прежним (SET updated_at = updated_at)
    assert "updated_at=chat_sessions.updated_at" in compiled.replace(" ", "")


def test_rename_foreign_session_is_404(api):
    c, db = api(_session(user_id=99))
    r = c.patch("/v1/sessions/5", json={"title": "x"}, headers={"Authorization": "x"})
    assert r.status_code == 404
    db.commit.assert_not_awaited()


def test_rename_missing_session_is_404(api):
    c, _ = api(None)
    assert (
        c.patch("/v1/sessions/5", json={"title": "x"}, headers={"Authorization": "x"}).status_code
        == 404
    )


@pytest.mark.parametrize("title", ["", "   ", "x" * 201])
def test_rename_validates_title(api, title):
    c, _ = api(_session())
    r = c.patch("/v1/sessions/5", json={"title": title}, headers={"Authorization": "x"})
    assert r.status_code == 422


def test_with_urls_adds_fresh_url_and_does_not_mutate(monkeypatch):
    monkeypatch.setattr(att_mod, "presign_get", lambda key, **kw: f"https://s/{key}")
    rec = {"type": "file", "name": "a.pdf", "key": OWN, "mime": "application/pdf", "size": 1}
    out = with_urls([rec])
    assert out[0]["url"] == f"https://s/{OWN}" and "url" not in rec
    assert with_urls(None) == [] and with_urls([]) == []


def test_with_urls_survives_unconfigured_storage(monkeypatch):
    from app.utils.r2 import StorageNotConfiguredError

    def broken(key, **kw):
        raise StorageNotConfiguredError("x")

    monkeypatch.setattr(att_mod, "presign_get", broken)
    out = with_urls([{"type": "file", "name": "a", "key": OWN, "mime": "m", "size": 1}])
    assert out[0]["url"] is None


async def test_resolve_attachment_keys_own_key_uses_object_metadata(monkeypatch):
    head = AsyncMock(return_value={"size": 99, "mime": "application/pdf", "name": "Договор.pdf"})
    monkeypatch.setattr(att_mod, "head_info_async", head)
    recs = await resolve_attachment_keys([OWN, OWN], 7)  # дубликаты схлопываются
    assert len(recs) == 1 and head.await_count == 1
    assert recs[0] == {
        "type": "file",
        "name": "Договор.pdf",
        "key": OWN,
        "mime": "application/pdf",
        "size": 99,
    }


@pytest.mark.parametrize(
    "key", ["ai/8/42/abc_f.pdf", "documents/x.pdf", "ai/7/../8/1/f.pdf", "ai/70/1/abc_f.pdf"]
)
async def test_resolve_attachment_keys_rejects_foreign(monkeypatch, key):
    monkeypatch.setattr(att_mod, "head_info_async", AsyncMock())
    with pytest.raises(HTTPException) as e:
        await resolve_attachment_keys([key], 7)
    assert e.value.status_code == 403


async def test_resolve_attachment_keys_missing_object_404(monkeypatch):
    monkeypatch.setattr(att_mod, "head_info_async", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as e:
        await resolve_attachment_keys([OWN], 7)
    assert e.value.status_code == 404


async def test_resolve_attachment_keys_even_admin_cannot_bind_foreign(monkeypatch):
    # resolve_attachment_keys не учитывает роль вообще: только owns_key
    monkeypatch.setattr(att_mod, "head_info_async", AsyncMock())
    with pytest.raises(HTTPException):
        await resolve_attachment_keys(["ai/8/1/abc_f.pdf"], 1)
