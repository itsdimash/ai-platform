import pytest
from fastapi.testclient import TestClient

from app.auth import CurrentUser, get_current_user
from app.main import app
from app.routers import files as files_router

OWN = "ai/7/42/0123456789abcdef0123456789abcdef_Отчёт.pdf"
FOREIGN = "ai/8/42/0123456789abcdef0123456789abcdef_secret.pdf"


@pytest.fixture
def client(monkeypatch):
    calls = {"presign": []}

    async def fake_head(key):
        if key.endswith("missing.pdf"):
            return None
        return {"size": 10, "mime": "application/pdf", "name": "Отчёт.pdf"}

    def fake_presign(key, expires=None, download_name=None, inline=None, content_type=None):
        calls["presign"].append(
            {"key": key, "download_name": download_name, "inline": inline, "ct": content_type}
        )
        return f"https://signed.example/{key}?sig=1"

    monkeypatch.setattr(files_router, "head_info_async", fake_head)
    monkeypatch.setattr(files_router, "presign_get", fake_presign)

    def login(user_id=7, role="pm"):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id=user_id, role=role)
        return TestClient(app), calls

    yield login
    app.dependency_overrides.clear()


def test_owner_gets_presigned_url_with_inline_for_pdf(client):
    c, calls = client()
    r = c.get(f"/v1/files/{OWN}/url")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"].startswith("https://signed.example/ai/7/42/") and body["expires_in"] > 0
    assert calls["presign"][0]["inline"] is True  # режим выбирается по mime из head_object
    assert calls["presign"][0]["download_name"] == "Отчёт.pdf"


def test_foreign_key_is_forbidden(client):
    c, calls = client()
    assert c.get(f"/v1/files/{FOREIGN}/url").status_code == 403
    assert calls["presign"] == []


@pytest.mark.parametrize(
    "key",
    ["documents/old/abc_f.docx", "erp/uploads/x.pdf", "ai/8/%2e%2e/7/42/f.pdf", "ai/7"],
)
def test_keys_outside_prefix_or_traversal_are_forbidden(client, key):
    # %2e%2e клиент не нормализует (а обычный '..' httpx схлопнул бы сам),
    # поэтому серверный запрет проверяем закодированной формой.
    c, _ = client()
    assert c.get(f"/v1/files/{key}/url").status_code == 403


@pytest.mark.parametrize("role", ["admin", "commercial_director"])
def test_privileged_roles_can_read_foreign_ai_key(client, role):
    c, _ = client(user_id=1, role=role)
    assert c.get(f"/v1/files/{FOREIGN}/url").status_code == 200


def test_privileged_role_still_cannot_read_outside_ai(client):
    c, _ = client(user_id=1, role="admin")
    assert c.get("/v1/files/documents/old/abc_f.docx/url").status_code == 403


def test_missing_object_is_404(client):
    c, _ = client()
    assert (
        c.get("/v1/files/ai/7/42/0123456789abcdef0123456789abcdef_missing.pdf/url").status_code
        == 404
    )


def test_requires_auth():
    app.dependency_overrides.clear()
    assert (
        TestClient(app).get(f"/v1/files/{OWN}/url").status_code == 422
    )  # нет заголовка Authorization
