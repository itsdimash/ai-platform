from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware import UploadSizeLimitMiddleware

LIMIT = 10_000


def make_client():
    seen = {"calls": 0}

    async def upload(request: Request):
        seen["calls"] += 1
        body = await request.body()
        return JSONResponse({"size": len(body)})

    async def other(request: Request):
        body = await request.body()
        return JSONResponse({"size": len(body)})

    app = Starlette(
        routes=[Route("/up", upload, methods=["POST"]), Route("/other", other, methods=["POST"])]
    )
    app.add_middleware(UploadSizeLimitMiddleware, limits_by_path={"/up": LIMIT})
    return TestClient(app), seen


def test_within_limit_passes():
    client, seen = make_client()
    r = client.post("/up", content=b"x" * (LIMIT - 1))
    assert r.status_code == 200 and r.json()["size"] == LIMIT - 1 and seen["calls"] == 1


def test_content_length_over_limit_rejected_before_handler():
    client, seen = make_client()
    r = client.post("/up", content=b"x" * (LIMIT + 1))
    assert r.status_code == 413 and "слишком большой" in r.json()["detail"]
    assert seen["calls"] == 0


def test_chunked_body_over_limit_is_cut_off():
    client, _ = make_client()

    def gen():
        for _ in range(20):
            yield b"x" * 1000

    r = client.post("/up", content=gen())  # без Content-Length
    assert r.status_code == 413


def test_other_paths_are_not_limited():
    client, _ = make_client()
    assert client.post("/other", content=b"x" * (LIMIT * 5)).status_code == 200
