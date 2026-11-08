"""Same-origin BFF routes. Provider and database work is performed through a port."""

import json
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from enterprise_ai_platform.api.errors import error_response
from enterprise_ai_platform.modules.identity.ports import IdentityFailure


router = APIRouter()
LOGIN_COOKIE = "__Host-eai-login"
SESSION_COOKIE = "__Host-eai-session"


def cookie(request, name):
    values = []
    for key, header in request.scope.get("headers", []):
        if key.lower() == b"cookie":
            for part in header.decode("latin-1").split(";"):
                item, separator, value = part.strip().partition("=")
                if item == name and separator:
                    values.append(value)
    return values[0] if len(values) == 1 else None


def browser_headers(request):
    result = {}
    for key, value in request.scope.get("headers", []):
        name = key.decode("latin-1").lower()
        if name in {"origin", "content-type", "sec-fetch-site", "x-eai-csrf-bootstrap", "x-csrf-token"}:
            result[name] = "__duplicate__" if name in result else value.decode("latin-1")
    return result


def set_cookie(response, name, value, seconds):
    response.set_cookie(name, value, max_age=seconds, secure=True, httponly=True, samesite="lax", path="/")


def clear_cookie(response, name):
    response.delete_cookie(name, secure=True, httponly=True, samesite="lax", path="/")


def service(request):
    instance = request.app.state.services.identity
    if instance is None:
        raise IdentityFailure(404)
    return instance


def failure(request, error, *, clear_session=False):
    response = error_response(error.status, request.state.request_id)
    if error.status == 429:
        response.headers["Retry-After"] = str(max(1, min(60, error.retry_after)))
    if clear_session and error.status == 401:
        clear_cookie(response, SESSION_COOKIE)
    return response


@router.get("/api/v1/auth/login")
async def login(request: Request):
    try:
        instance = service(request)
        try:
            pairs = parse_qsl(request.scope.get("query_string", b"").decode("ascii"),
                              keep_blank_values=True, max_num_fields=2, errors="strict")
            if len(pairs) > 1 or any(k != "return_to" for k, _ in pairs):
                raise ValueError
        except (ValueError, UnicodeError):
            pairs = [("return_to", "__invalid__")]
        result = await instance.login(dict(pairs).get("return_to", "home"), cookie(request, LOGIN_COOKIE),
                                      request.client.host if request.client else "")
        response = RedirectResponse(result["location"], status_code=303)
        set_cookie(response, LOGIN_COOKIE, result["binding"], 300)
        return response
    except IdentityFailure as error:
        return failure(request, error)


@router.get("/api/v1/auth/callback")
async def callback(request: Request):
    try:
        result = await service(request).callback(request.scope.get("query_string", b""), cookie(request, LOGIN_COOKIE),
            cookie(request, SESSION_COOKIE), request.client.host if request.client else "")
        response = RedirectResponse(result["location"], status_code=303)
        set_cookie(response, SESSION_COOKIE, result["token"], result["max_age"])
        clear_cookie(response, LOGIN_COOKIE)
        return response
    except IdentityFailure as error:
        return failure(request, error)


@router.get("/api/v1/me")
async def me(request: Request):
    try:
        body = await service(request).me(cookie(request, SESSION_COOKIE), browser_headers(request))
        return JSONResponse(body)
    except IdentityFailure as error:
        return failure(request, error, clear_session=True)


async def object_body(request):
    try:
        raw = await request.body()
        if len(raw) > 4096:
            raise ValueError
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        body = json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(body, dict):
            raise ValueError
        return body
    except (ValueError, UnicodeError):
        raise IdentityFailure(400) from None


@router.post("/api/v1/auth/logout")
async def logout(request: Request):
    try:
        instance = service(request)
        if await object_body(request) != {}:
            raise IdentityFailure(400)
        await instance.logout(cookie(request, SESSION_COOKIE), browser_headers(request))
        response = Response(status_code=204)
        clear_cookie(response, SESSION_COOKIE)
        return response
    except IdentityFailure as error:
        return failure(request, error, clear_session=True)


@router.post("/api/v1/session/tenant")
async def switch(request: Request):
    try:
        instance = service(request)
        body = await object_body(request)
        if set(body) != {"membership_id"} or not isinstance(body["membership_id"], str):
            raise IdentityFailure(400)
        result = await instance.switch(cookie(request, SESSION_COOKIE), browser_headers(request), body["membership_id"])
        response = JSONResponse(result["body"])
        set_cookie(response, SESSION_COOKIE, result["token"], result["max_age"])
        return response
    except IdentityFailure as error:
        return failure(request, error, clear_session=True)
