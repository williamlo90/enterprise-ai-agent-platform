"""Function URL v2 adapter for the real API; fresh application and loop per invocation."""

import asyncio
import base64
from contextlib import closing
import ipaddress
import json
import logging
import os
import re
import time
from urllib.parse import unquote, urlsplit
from uuid import uuid4
from pathlib import Path

import boto3
from botocore.config import Config

from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.api.frontend_assets import frontend_response
from enterprise_ai_platform.integrations.identity.cognito_probe import safe_transport_logs, strict_json


PATHS={"/api/v1/auth/login":"login","/api/v1/auth/callback":"callback","/api/v1/me":"me",
       "/api/v1/auth/logout":"logout","/api/v1/session/tenant":"tenant_switch","/":"home",
       "/health/live":"health_live","/health/ready":"health_ready","/identity-check.js":"demo_script"}
logger=logging.getLogger("enterprise_ai_platform.lambda_api")


class InvalidInvocation(ValueError):
    pass


class BodyTooLarge(ValueError):
    pass


def rejection(status):
    request_id=str(uuid4())
    code,message={400:("invalid_request","Invalid request."),404:("not_found","Resource not found."),
                  405:("method_not_allowed","Method not allowed."),413:("body_too_large","Request body exceeds the allowed size."),
                  503:("service_unavailable","Service temporarily unavailable.")}[status]
    return {"statusCode":status,"headers":{"content-type":"application/json","cache-control":"no-store",
        "referrer-policy":"no-referrer","content-security-policy":"default-src 'none'; frame-ancestors 'none'","x-request-id":request_id},
        "body":json.dumps({"error":{"code":code,"message":message,"request_id":request_id,"retryable":status==503}})}


def request_scope(event, origin):
    if event.get("version")!="2.0":raise ValueError("Unsupported invocation.")
    http=event["requestContext"]["http"]
    ip=str(ipaddress.ip_address(http["sourceIp"]))
    method=http["method"]
    if method not in {"GET","POST","PUT","PATCH","DELETE","HEAD","OPTIONS"}:raise ValueError("Invalid method.")
    path=event["rawPath"];query=event.get("rawQueryString","")
    if not isinstance(path,str) or not path.startswith("/") or len(path.encode())>2048:raise ValueError("Invalid path.")
    if not isinstance(query,str) or len(query.encode())>8192:raise ValueError("Invalid query.")
    raw_headers=event.get("headers",{})
    if not isinstance(raw_headers,dict) or len(raw_headers)>100:raise ValueError("Invalid headers.")
    headers=[]
    for k,v in raw_headers.items():
        if not isinstance(k,str) or not isinstance(v,str) or len(v.encode())>16384:raise ValueError("Invalid header.")
        if k.lower() in {"x-forwarded-for","x-forwarded-proto","forwarded"}:continue
        if k.lower()=="cookie" and "cookies" in event:continue
        headers.append((k.lower().encode("ascii"),v.encode("latin-1")))
    if "cookies" in event:
        cookies=event["cookies"]
        if not isinstance(cookies,list) or len(cookies)>100 or any(not isinstance(x,str) for x in cookies):raise ValueError("Invalid cookies.")
        value="; ".join(cookies)
        if len(value.encode())>16384:raise ValueError("Invalid cookies.")
        headers.append((b"cookie",value.encode("latin-1")))
    body=event.get("body") or ""
    if not isinstance(body,str):raise ValueError("Invalid body.")
    if len(body)>1500000:raise BodyTooLarge()
    body=base64.b64decode(body,validate=True) if event.get("isBase64Encoded") is True else body.encode("utf-8")
    if len(body)>1048576:raise BodyTooLarge()
    return {"type":"http","asgi":{"version":"3.0","spec_version":"2.3"},"http_version":"1.1","scheme":"https",
        "method":method,"path":unquote(path,errors="strict"),"raw_path":path.encode("ascii"),"query_string":query.encode("ascii"),
        "root_path":"","headers":headers,"client":(ip,0),"server":(urlsplit(origin).hostname,443)},body


async def invoke_asgi(app,event,origin,*,work_deadline):
    scope,body=request_scope(event,origin)
    received=False;status=None;headers=[];content=bytearray()
    async def receive():
        nonlocal received
        if not received:
            received=True
            return {"type":"http.request","body":body,"more_body":False}
        await asyncio.Event().wait()
    async def send(message):
        nonlocal status,headers
        if message["type"]=="http.response.start":
            status=message["status"];headers=message.get("headers",[])
        elif message["type"]=="http.response.body":
            content.extend(message.get("body",b""))
            if len(content)>1048576:raise ValueError("Response exceeds allowed size.")
    # On cancellation the existing bounded rollback/release/pool-close code runs
    # inside the separate cleanup reserve. No app/pool/task survives this loop.
    async with asyncio.timeout_at(work_deadline):
        async with app.router.lifespan_context(app):
            await app(scope,receive,send)
    if status is None:raise ValueError("Response unavailable.")
    result={"statusCode":status,"headers":{},"cookies":[],"body":base64.b64encode(content).decode(),"isBase64Encoded":True}
    for k,v in headers:
        name=k.decode("latin-1").lower();value=v.decode("latin-1")
        if name=="set-cookie":result["cookies"].append(value)
        elif name not in {"connection","transfer-encoding"}:result["headers"][name]=value
    return result


def load_application():
    if os.environ.get('APP_FRONTEND_ENABLED') == 'true' and os.environ.get('APP_BROWSER_CONTEXT_ENABLED') != 'true':
        raise ValueError('Frontend requires browser context readiness.')
    with closing(boto3.client("secretsmanager",region_name="ap-southeast-1",config=Config(
            connect_timeout=1,read_timeout=2,retries={"total_max_attempts":1}))) as client:
        private=strict_json(client.get_secret_value(SecretId=os.environ["SPIKE_SECRET_ARN"])["SecretString"].encode())
    # Only these private fields become application settings. Never load a direct URL.
    fields={k:os.environ[k] for k in ("OIDC_ISSUER","OIDC_CLIENT_ID","OIDC_REDIRECT_URI","PUBLIC_ORIGIN",
            "OIDC_AUTHORIZATION_ENDPOINT","OIDC_TOKEN_ENDPOINT","OIDC_JWKS_URI")}
    settings=Settings(env="development",host="127.0.0.1",port=8000,database_enabled=True,database_pool_max_size=1,
        auth_enabled=True,auth_callback_logging_controlled=os.environ.get("APP_AUTH_CALLBACK_LOGGING_CONTROLLED")=="true",
        auth_jwks_background_refresh=False,auth_demo_ui=os.environ.get("APP_AUTH_DEMO_UI")=="true" and os.environ.get("APP_FRONTEND_ENABLED")!="true",
        browser_context_enabled=os.environ.get("APP_BROWSER_CONTEXT_ENABLED", "false"),
        conversations_enabled=False,knowledge_enabled=False,files_enabled=False,internal_auth_enabled=False,
        auth_verifier_key=private["auth_verifier_key"],DATABASE_URL=private["database_url"],
        OIDC_CLIENT_SECRET=private["client_secret"],**fields)
    return create_app(settings),settings.public_origin


def handler(event,context):
    safe_transport_logs()
    logger.setLevel(logging.INFO)
    request_id=getattr(context,"aws_request_id","")
    if not isinstance(request_id,str) or not re.fullmatch(r"[A-Za-z0-9-]{1,80}",request_id):request_id=str(uuid4())
    path=event.get("rawPath") if isinstance(event,dict) else None
    kind=PATHS.get(path,"other") if isinstance(path,str) else "other"
    result=rejection(503)
    stage="disabled"
    try:
        frontend = os.environ.get('APP_FRONTEND_ENABLED') == 'true'
        if os.environ.get("APP_AUTH_ENABLED")!="true" and not frontend:
            result=rejection(404)
        else:
            stage="request_validation"
            try:
                request_scope(event,os.environ.get("PUBLIC_ORIGIN","https://invalid.example.test"))
            except BodyTooLarge:
                raise
            except (ValueError,UnicodeError,TypeError,KeyError,AttributeError):
                raise InvalidInvocation() from None
            if frontend:
                public = frontend_response(event, Path(__file__).resolve().parent.parent / '_frontend')
                if public is not None:
                    result = rejection(public) if isinstance(public, int) else public
                    result.setdefault('headers', {})['x-eai-invocation-id'] = request_id
                    # Fixed category only; never log path/query, including rejected asset probes.
                    logger.info('identity_request kind=frontend stage=public request_id=%s status=%s', request_id, result['statusCode'])
                    return result
            if os.environ.get('APP_AUTH_ENABLED') != 'true':
                result = rejection(404)
                result['headers']['x-eai-invocation-id'] = request_id
                logger.info('identity_request kind=%s stage=disabled request_id=%s status=%s', kind, request_id, result['statusCode'])
                return result
            remaining=context.get_remaining_time_in_millis()/1000
            if remaining<35:raise TimeoutError("Insufficient runtime budget.")
            # At most 30 seconds of work; up to 26 seconds reserved for the
            # adapter's sequential bounded rollback/release/pool-close paths.
            work_deadline=time.monotonic()+min(30,remaining-27)
            stage="settings"
            app,origin=load_application()
            stage="request"
            with asyncio.Runner(loop_factory=database_loop_factory) as runner:
                result=runner.run(invoke_asgi(app,event,origin,work_deadline=work_deadline))
    except BodyTooLarge:
        result=rejection(413)
    except InvalidInvocation:
        result=rejection(400)
    except Exception:
        result=rejection(503)
    result.setdefault("headers",{})["x-eai-invocation-id"]=request_id
    logger.info("identity_request kind=%s stage=%s request_id=%s status=%s",kind,stage,request_id,result["statusCode"])
    return result
