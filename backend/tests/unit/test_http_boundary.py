import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from enterprise_ai_platform.api.boundary import RequestBoundary, ResponseInterrupted
from enterprise_ai_platform.api.schemas import RequestModel
from enterprise_ai_platform.config import ConfigurationError, Settings, load_settings
from enterprise_ai_platform.main import create_app

PRIVATE = "synthetic-private-marker"


class Input(RequestModel):
    name: str
    count: int


@pytest.fixture
def app():
    app = create_app(Settings(env="test", host="127.0.0.1", port=8000))

    @app.post("/_test/input")
    async def input_route(payload: Input):
        return {"accepted": True}

    @app.get("/_test/http-error/{code}")
    async def error_route(code: int):
        raise HTTPException(code, detail=PRIVATE, headers={"X-Private": PRIVATE})

    @app.get("/_test/crash")
    async def crash_route():
        raise RuntimeError(PRIVATE)

    @app.get("/_test/timeout")
    async def timeout_route():
        raise TimeoutError(PRIVATE)

    @app.get("/_test/invalid-output", response_model=Input)
    async def invalid_output():
        return {"name": PRIVATE, "count": PRIVATE}

    return app


def check_error(response, code):
    assert response.status_code == code
    error = response.json()["error"]
    assert set(error) == {"code", "message", "request_id", "retryable"}
    assert error["request_id"] == response.headers["X-Request-ID"]
    UUID(error["request_id"])
    assert PRIVATE not in response.text + str(response.headers)
    return error


def test_health_lifecycle_and_readiness_is_not_dependency_health(app):
    assert app.state.ready is False
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "ok", "scope": "process"}
        assert client.get("/health/ready").json() == {"status": "ready", "scope": "bootstrap"}
        app.state.ready = False
        assert check_error(client.get("/health/ready"), 503)["retryable"] is True
        assert client.get("/health/live").status_code == 200
    assert app.state.ready is False


@pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 415, 422, 429, 500, 503])
def test_http_errors_do_not_echo_details_or_arbitrary_headers(app, code):
    with TestClient(app) as client:
        check_error(client.get(f"/_test/http-error/{code}"), code)


def test_unknown_route_and_method_use_envelope(app):
    with TestClient(app) as client:
        check_error(client.get("/" + PRIVATE), 404)
        result = client.post("/health/live")
        check_error(result, 405)
        assert result.headers["Allow"] == "GET"


@pytest.mark.parametrize("payload", [
    {"name": "ok", "count": 1, "extra": PRIVATE},
    {"name": PRIVATE, "count": "1"},
    {"name": PRIVATE},
    {"name": "a" * 4097, "count": 1},
])
def test_strict_request_field_limits_and_no_validation_input_echo(app, payload):
    with TestClient(app) as client:
        check_error(client.post("/_test/input", json=payload), 422)


def test_exact_text_limit_valid_and_malformed_json_safe(app):
    with TestClient(app) as client:
        assert client.post("/_test/input", json={"name": "a" * 4096, "count": 1}).status_code == 200
        check_error(client.post("/_test/input", content='{"' + PRIVATE, headers={"Content-Type": "application/json"}), 400)


@pytest.mark.parametrize("path", ["/_test/crash", "/_test/timeout", "/_test/invalid-output"])
def test_unhandled_errors_sanitized_with_correlation_and_no_input_in_logs(app, caplog, path):
    caplog.set_level(logging.INFO, logger="enterprise_ai_platform.http")
    with TestClient(app) as client:
        response = client.get(path + "?token=" + PRIVATE)
        error = check_error(response, 500)
    assert error["request_id"] in caplog.text
    assert PRIVATE not in caplog.text
    assert "Traceback" not in caplog.text


def test_request_ids_are_server_owned_and_distinct_concurrently(app):
    with TestClient(app) as client:
        def request(_):
            return client.get("/health/live", headers={"X-Request-ID": PRIVATE}).headers["X-Request-ID"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(request, range(24)))
    assert len(set(ids)) == 24
    for value in ids:
        UUID(value)
        assert value != PRIVATE


async def asgi_call(app, chunks, headers=(), receive_delay=0, captured=None):
    messages = [] if captured is None else captured
    pending = iter(chunks)

    async def receive():
        if receive_delay:
            await asyncio.sleep(receive_delay)
        return next(pending, {"type": "http.disconnect"})

    async def send(message):
        messages.append(message)

    await app({"type": "http", "headers": list(headers), "state": {}}, receive, send)
    return messages


async def accept_body(scope, receive, send):
    body = await receive()
    await JSONResponse({"received": len(body["body"])})(scope, receive, send)


def parts(body, more=False):
    return {"type": "http.request", "body": body, "more_body": more}


@pytest.mark.parametrize("headers,chunks,expected", [
    ([], [parts(b"12345")], 200),
    ([], [parts(b"123", True), parts(b"456")], 413),
    ([(b"content-length", b"6")], [], 413),
    ([(b"content-length", b"5")], [parts(b"123", True), parts(b"45")], 200),
    ([(b"content-length", b"2")], [parts(b"123")], 400),
    ([(b"content-length", b"bad")], [], 400),
    ([(b"content-length", b"-1")], [], 400),
    ([(b"content-length", b"2"), (b"content-length", b"2")], [], 400),
    ([(b"content-length", b"9" * 1000)], [], 400),
])
def test_asgi_bounded_body_chunking_and_content_length(headers, chunks, expected):
    app = RequestBoundary(accept_body, max_body_bytes=5, body_timeout=1)
    messages = asyncio.run(asgi_call(app, chunks, headers))
    assert messages[0]["status"] == expected
    if expected != 200:
        error = json.loads(messages[1]["body"])["error"]
        assert error["request_id"].encode() == dict(messages[0]["headers"])[b"x-request-id"]


def test_receive_deadline_and_disconnect_do_not_run_route():
    app = RequestBoundary(accept_body, max_body_bytes=5, body_timeout=0.01)
    messages = asyncio.run(asgi_call(app, [parts(b"1")], receive_delay=0.1))
    assert messages[0]["status"] == 408
    assert asyncio.run(asgi_call(app, [{"type": "http.disconnect"}])) == []


def test_after_response_start_failure_is_sanitized_without_second_response(caplog):
    async def failing(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise ValueError(PRIVATE)

    app = RequestBoundary(failing, max_body_bytes=5, body_timeout=1)
    messages = []
    with pytest.raises(ResponseInterrupted, match="Response interrupted") as exc:
        asyncio.run(asgi_call(app, [parts(b"")], captured=messages))
    assert sum(m["type"] == "http.response.start" for m in messages) == 1
    assert len(messages) == 2
    assert exc.value.__suppress_context__ is True
    assert PRIVATE not in str(exc.value) + caplog.text


def test_body_limit_applies_even_to_unknown_routes():
    app = create_app(Settings(env="test", host="127.0.0.1", port=8000, max_request_body_bytes=5))
    with TestClient(app) as client:
        check_error(client.post("/" + PRIVATE, content=b"123456"), 413)


@pytest.mark.parametrize("key,value", [
    ("APP_MAX_REQUEST_BODY_BYTES", "0"),
    ("APP_MAX_REQUEST_BODY_BYTES", "16777217"),
    ("APP_MAX_REQUEST_BODY_BYTES", PRIVATE),
    ("APP_REQUEST_BODY_TIMEOUT_SECONDS", "0"),
    ("APP_REQUEST_BODY_TIMEOUT_SECONDS", "61"),
])
def test_invalid_boundary_config_is_sanitized(monkeypatch, key, value):
    for k, v in {"APP_ENV": "test", "APP_HOST": "127.0.0.1", "APP_PORT": "8000", key: value}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ConfigurationError) as exc:
        load_settings(None)
    assert key in str(exc.value)
    assert PRIVATE not in str(exc.value)
