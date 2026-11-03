"""ASGI input limits, server-owned correlation and sanitized failure handling."""

import asyncio
import logging
import time
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from enterprise_ai_platform.api.errors import error_response

logger = logging.getLogger("enterprise_ai_platform.http")


class ResponseInterrupted(RuntimeError):
    """Safe error for a response that cannot be replaced after headers were sent."""


class RequestBodyTimeout(TimeoutError):
    """Only input-receive deadlines map to HTTP 408."""


class RequestBoundary:
    def __init__(self, app: ASGIApp, max_body_bytes: int, body_timeout: float):
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.body_timeout = body_timeout

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = False
        status = 500
        began = time.monotonic()

        async def correlated_send(message: Message) -> None:
            nonlocal started, status
            if message["type"] == "http.response.start":
                headers = [
                    (key, value) for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
                started = True
                status = message["status"]
            await send(message)

        async def reject(code: int) -> None:
            await error_response(code, request_id)(scope, receive, correlated_send)

        try:
            lengths = [v for k, v in scope.get("headers", []) if k.lower() == b"content-length"]
            if lengths:
                raw = lengths[0]
                if len(lengths) != 1 or not raw or len(raw) > 20 or any(c < 48 or c > 57 for c in raw):
                    await reject(400)
                    return
                if int(raw) > self.max_body_bytes:
                    await reject(413)
                    return
            body = bytearray()
            try:
                async with asyncio.timeout(self.body_timeout):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            status = 499
                            return
                        chunk = message.get("body", b"")
                        if len(body) + len(chunk) > self.max_body_bytes:
                            await reject(413)
                            return
                        body.extend(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                raise RequestBodyTimeout from None
            if lengths and len(body) != int(lengths[0]):
                await reject(400)
                return
            replayed = False

            async def replay_body() -> Message:
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, replay_body, correlated_send)
        except RequestBodyTimeout:
            if started:
                raise ResponseInterrupted("Response interrupted.") from None
            await reject(408)
        except Exception:
            logger.error("request_failed request_id=%s", request_id)
            if started:
                raise ResponseInterrupted("Response interrupted.") from None
            await reject(500)
        finally:
            # No raw path, query, headers, body, or exception text in operational logs.
            logger.info(
                "request_complete request_id=%s status=%s duration_ms=%.2f",
                request_id, status, (time.monotonic() - began) * 1000,
            )
