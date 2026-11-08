"""Function URL bridge fidelity, lifecycle isolation and cancellation without AWS."""

import asyncio
import base64
from contextlib import asynccontextmanager
import json
import time

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse
import pytest

from enterprise_ai_platform import lambda_api


ORIGIN="https://fixture.lambda-url.ap-southeast-1.on.aws"


def event(**changes):
    result={"version":"2.0","rawPath":"/test","rawQueryString":"state=one&state=two",
        "requestContext":{"http":{"method":"GET","sourceIp":"198.51.100.20"}},
        "headers":{"host":"fixture.lambda-url.ap-southeast-1.on.aws","x-forwarded-for":"203.0.113.99"},
        "cookies":["a=one","b=two"],"body":"","isBase64Encoded":False}
    result.update(changes)
    return result


def test_asgi_bridge_preserves_raw_duplicates_cookies_and_authoritative_source_ip():
    app=FastAPI()
    @app.get("/test")
    async def echo(request:Request):
        assert request.scope["query_string"]==b"state=one&state=two"
        assert request.client.host=="198.51.100.20" and "x-forwarded-for" not in request.headers
        assert request.cookies=={"a":"one","b":"two"}
        response=JSONResponse({"ok":True})
        response.set_cookie("__Host-eai-session","synthetic",secure=True,httponly=True,path="/",samesite="lax")
        response.delete_cookie("__Host-eai-login",secure=True,httponly=True,path="/",samesite="lax")
        return response
    result=asyncio.run(lambda_api.invoke_asgi(app,event(),ORIGIN,work_deadline=time.monotonic()+3))
    assert result["statusCode"]==200 and len(result["cookies"])==2
    assert "set-cookie" not in result["headers"]
    assert json.loads(base64.b64decode(result["body"]))=={"ok":True}


def test_request_cancellation_completes_lifespan_cleanup():
    state=[]
    @asynccontextmanager
    async def lifetime(app):
        state.append("open")
        try:yield
        finally:
            await asyncio.sleep(0.01)
            state.append("closed")
    app=FastAPI(lifespan=lifetime)
    @app.get("/test")
    async def slow():await asyncio.sleep(1)
    with pytest.raises(TimeoutError):
        asyncio.run(lambda_api.invoke_asgi(app,event(),ORIGIN,work_deadline=time.monotonic()+0.02))
    assert state==["open","closed"]


def test_handler_creates_and_closes_a_new_application_per_invocation(monkeypatch):
    state=[]
    def load():
        index=len(state);state.append([])
        @asynccontextmanager
        async def lifetime(app):
            state[index].append("open")
            try:yield
            finally:state[index].append("close")
        app=FastAPI(lifespan=lifetime)
        @app.get("/test")
        async def ok():return {"ok":True}
        return app,ORIGIN
    class Context:
        aws_request_id="synthetic-invocation"
        def get_remaining_time_in_millis(self):return 60000
    monkeypatch.setenv("APP_AUTH_ENABLED","true")
    monkeypatch.setattr(lambda_api,"load_application",load)
    for _ in range(2):assert lambda_api.handler(event(),Context())["statusCode"]==200
    assert state==[["open","close"],["open","close"]]


def test_disabled_handler_does_not_load_any_secret(monkeypatch,caplog):
    monkeypatch.setattr(lambda_api,"load_application",lambda:pytest.fail("Disabled handler must not load configuration"))
    result=lambda_api.handler(event(rawQueryString="code=private-marker"),None)
    assert result["statusCode"]==404 and "private-marker" not in caplog.text


@pytest.mark.parametrize("incoming",[None,{"version":"1.0"},event(rawPath=[]),event(rawQueryString="x"*8193),
    event(body="not-valid-base64!",isBase64Encoded=True)])
def test_malformed_invocations_fail_before_loading_credentials(monkeypatch,incoming,caplog):
    monkeypatch.setenv("APP_AUTH_ENABLED","true")
    monkeypatch.setattr(lambda_api,"load_application",lambda:pytest.fail("Malformed request must not read credentials"))
    result=lambda_api.handler(incoming,None)
    assert result["statusCode"]==400
    assert json.loads(result["body"])["error"]["request_id"]==result["headers"]["x-request-id"]


def test_configuration_failure_is_503_without_exception_details(monkeypatch,caplog):
    class Context:
        aws_request_id="synthetic-invocation"
        def get_remaining_time_in_millis(self):return 60000
    def fail():raise ValueError("private-marker")
    monkeypatch.setenv("APP_AUTH_ENABLED","true")
    monkeypatch.setattr(lambda_api,"load_application",fail)
    result=lambda_api.handler(event(),Context())
    assert result["statusCode"]==503
    assert "private-marker" not in str(result) and "private-marker" not in caplog.text


@pytest.mark.parametrize("encoded",[False,True])
@pytest.mark.parametrize("size",[1048576,1048577])
def test_exact_and_over_body_limits_preserve_413_semantics(monkeypatch,encoded,size):
    data=b"x"*size
    body=base64.b64encode(data).decode() if encoded else data.decode()
    incoming=event(body=body,isBase64Encoded=encoded)
    if size==1048576:
        assert lambda_api.request_scope(incoming,ORIGIN)[1]==data
    else:
        with pytest.raises(lambda_api.BodyTooLarge):lambda_api.request_scope(incoming,ORIGIN)
        monkeypatch.setenv("APP_AUTH_ENABLED","true")
        monkeypatch.setattr(lambda_api,"load_application",lambda:pytest.fail("Oversized body must not read credentials"))
        result=lambda_api.handler(incoming,None)
        assert result["statusCode"]==413 and json.loads(result["body"])["error"]["code"]=="body_too_large"
