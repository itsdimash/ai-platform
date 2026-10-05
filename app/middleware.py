"""Ранний отказ для слишком больших загрузок.

FastAPI разбирает multipart-тело ДО вызова хендлера, поэтому проверки внутри
эндпоинта срабатывают уже после того, как тело принято. Этот ASGI-middleware
отвечает 413 сразу по Content-Length, а для тела без Content-Length
(chunked) прерывает приём, как только превышен лимит.
"""

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class _TooLargeError(Exception):
    pass


class UploadSizeLimitMiddleware:
    def __init__(self, app, limits_by_path: dict[str, int]):
        self.app = app
        self.limits_by_path = limits_by_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = (
            self.limits_by_path.get(scope["path"])
            if scope["type"] == "http" and scope["method"] == "POST"
            else None
        )
        if limit is None:
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(send, limit)
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLargeError:
            if not response_started:
                await self._reject(send, limit)

    @staticmethod
    async def _reject(send: Send, limit: int) -> None:
        body = json.dumps(
            {"detail": f"Запрос слишком большой (лимит {limit // (1024 * 1024)} МБ)"}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
