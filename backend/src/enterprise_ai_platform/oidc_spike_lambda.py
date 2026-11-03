"""Standalone, operator-gated AWS probe. No application login/session is created."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from urllib.parse import parse_qsl, urlencode

import boto3
from botocore.config import Config
from cryptography.fernet import Fernet

from enterprise_ai_platform.config import OidcProbeSettings
from enterprise_ai_platform.infrastructure.oidc_spike_store import DynamoSpikeStore, StoreRejected
from enterprise_ai_platform.integrations.identity.cognito_probe import (
    CognitoProbe, OidcRejected, safe_transport_logs, strict_json, validate_keys, verify_id_token,
)
from enterprise_ai_platform.modules.identity.domain import CognitoProvider, ProbeDeadline, nonce_digest


COOKIE = "__Host-eai-spike"
HTML = """<!doctype html><html lang="en"><meta charset="utf-8"><title>Cognito feasibility probe</title>
<h1>Cognito feasibility probe</h1><p>This checks provider integration only. No platform session or tenant access is created.</p>
<label>Local probe operator key <input id="key" type="password" autocomplete="off"></label>
<button id="start">Sign in with Cognito</button><p id="message"></p>
<script src="/spike/launcher.js"></script></html>"""
SCRIPT = """document.getElementById('start').onclick=async()=>{
const input=document.getElementById('key'); const key=input.value; input.value='';
const response=await fetch('/spike/start',{method:'POST',headers:{'Content-Type':'application/json','X-EAI-Spike-Operator':key},body:'{}',cache:'no-store'});
if(response.ok){const data=await response.json();window.location.assign(data.authorization_url);}
else{document.getElementById('message').textContent='Probe could not start. Check configuration or retry later.';}};"""


def digest(kind, value):
    return hashlib.sha256(("eai:spike:" + kind + ":v1:" + value).encode("ascii")).hexdigest()


def response(status, body, *, content_type="application/json", location=None, cookie=None):
    headers = {"content-type": content_type, "cache-control": "no-store", "referrer-policy": "no-referrer",
               "content-security-policy": "default-src 'none'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
               "x-content-type-options": "nosniff"}
    if location:
        headers["location"] = location
    result = {"statusCode": status, "headers": headers,
              "body": json.dumps(body) if content_type == "application/json" else body}
    if cookie:
        result["cookies"] = [f"{COOKIE}={cookie}; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=300"]
    return result


def binding_cookie(event):
    values = []
    for cookie in event.get("cookies", []):
        for part in cookie.split(";"):
            name, separator, value = part.strip().partition("=")
            if name == COOKIE and separator:
                values.append(value)
    if len(values) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{43}", values[0]):
        return None
    return values[0]


class SpikeApplication:
    def __init__(self, provider, operator_key, encryption_key, store, *, probe=None, clock=time.time, deadline=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", operator_key):
            raise OidcRejected()
        self.provider, self.operator_key = provider, operator_key
        self.cipher, self.store = Fernet(encryption_key), store
        self.deadline = deadline or ProbeDeadline(time.monotonic() + 24)
        self.probe, self.clock = probe or CognitoProbe(provider, deadline=self.deadline), clock

    async def handle(self, event):
        now = int(self.clock())
        method = event.get("requestContext", {}).get("http", {}).get("method", "")
        path, raw = event.get("rawPath", ""), event.get("rawQueryString", "")
        headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
        if len(raw.encode()) > 8192:
            return response(400, {"status": "rejected"})
        if path != "/api/v1/auth/callback" and raw:
            return response(400, {"status": "rejected"})
        if method == "GET" and path == "/health/live":
            return response(200, {"status": "ok", "scope": "oidc_spike_only"})
        if method == "GET" and path == "/":
            return response(200, HTML, content_type="text/html; charset=utf-8")
        if method == "GET" and path == "/spike/launcher.js":
            return response(200, SCRIPT, content_type="text/javascript; charset=utf-8")
        binding = binding_cookie(event)
        key = "tx#" + digest("binding", binding) if binding else None
        if method == "POST" and path == "/spike/start":
            supplied = headers.get("x-eai-spike-operator", "")
            if (len(supplied) != 43 or not hmac.compare_digest(supplied, self.operator_key)
                    or headers.get("origin") != self.provider.public_origin
                    or headers.get("sec-fetch-site") != "same-origin"
                    or headers.get("content-type") != "application/json"):
                return response(403, {"status": "rejected"})
            body = event.get("body") or ""
            try:
                if len(body.encode()) > 8192:
                    raise OidcRejected()
                if event.get("isBase64Encoded"):
                    body = base64.b64decode(body, validate=True).decode("utf-8")
                valid_body = len(body.encode()) <= 4096 and strict_json(body.encode()) == {}
            except (ValueError, UnicodeError, OidcRejected):
                valid_body = False
            if not valid_body:
                return response(400, {"status": "rejected"})
            self.store.limit(now)
            state, nonce, verifier, new_binding = [secrets.token_urlsafe(32) for _ in range(4)]
            row = {"pk": "tx#" + digest("binding", new_binding), "state_hash": digest("state", state),
                   "nonce_hash": nonce_digest(nonce), "verifier": self.cipher.encrypt(verifier.encode()).decode(),
                   "expires_at": now + 300, "status": "pending"}
            self.store.start(row, key)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            url = self.provider.authorization_endpoint + "?" + urlencode({
                "response_type": "code", "client_id": self.provider.client_id,
                "redirect_uri": self.provider.redirect_uri, "scope": "openid profile",
                "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
            })
            return response(200, {"authorization_url": url}, cookie=new_binding)
        if method == "GET" and path == "/spike/result":
            if not key:
                return response(401, {"status": "rejected"})
            self.store.limit(now)
            return response(200, {"provider_verified": self.store.result(key, int(self.clock())),
                                  "application_session_created": False})
        if method == "GET" and path == "/api/v1/auth/callback":
            try:
                if re.search(r"%(?![a-fA-F0-9]{2})", raw):
                    raise ValueError
                pairs = parse_qsl(raw, keep_blank_values=True, max_num_fields=10, encoding="utf-8", errors="strict")
            except (ValueError, UnicodeError):
                return response(400, {"status": "rejected"})
            params = dict(pairs)
            if (not key or len(params) != len(pairs) or set(params) - {"code", "state", "iss", "error", "error_description"}
                    or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.get("state", ""))
                    or ("iss" in params and params["iss"] != self.provider.issuer)
                    or ("code" in params) == ("error" in params)
                    or ("code" in params and not 1 <= len(params["code"].encode()) <= 4096)):
                return response(400, {"status": "rejected"})
            self.store.limit(now)
            row = self.store.claim(key, digest("state", params["state"]), int(self.clock()))
            success = False
            try:
                if "error" not in params:
                    self.deadline.remaining(reserve=3)
                    if row["expires_at"] <= int(self.clock()):
                        raise OidcRejected()
                    verifier = self.cipher.decrypt(row["verifier"].encode(), ttl=300).decode()
                    async with asyncio.timeout(self.deadline.remaining(reserve=3)):
                        status, tokens = await self.probe.request("POST", self.provider.token_endpoint, basic=True, form={
                            "grant_type": "authorization_code", "code": params["code"],
                            "redirect_uri": self.provider.redirect_uri, "code_verifier": verifier,
                        })
                    self.deadline.remaining(reserve=3)
                    if status != 200 or not isinstance(tokens, dict):
                        raise OidcRejected()
                    async with asyncio.timeout(self.deadline.remaining(reserve=3)):
                        status, document = await self.probe.request("GET", self.provider.jwks_uri)
                    self.deadline.remaining(reserve=3)
                    if status != 200:
                        raise OidcRejected()
                    keys = validate_keys(document)
                    verify_id_token(tokens.get("id_token"), keys, self.provider, row["nonce_hash"], int(self.clock()))
                    success = True
            except Exception:
                # No token, code, claims, raw error or browser data is logged or persisted.
                success = False
            self.store.finish(key, success)
            return response(303, "", location="/spike/result")
        return response(404, {"status": "not_found"})


def handler(event, context):
    # SDK debug log records can contain signed headers or wire payloads.
    safe_transport_logs()
    try:
        if os.environ.get("SPIKE_ENABLED") != "true":
            return response(404, {"status": "not_found"})
        remaining = context.get_remaining_time_in_millis() / 1000 if context is not None else 25
        deadline = ProbeDeadline(time.monotonic() + min(24, remaining - 1))
        left = deadline.remaining(reserve=3)
        with boto3.client("secretsmanager", region_name="ap-southeast-1", config=Config(
            connect_timeout=min(1, left / 2), read_timeout=min(1, left / 2), retries={"total_max_attempts": 1},
        )) as secrets_client:
            secret_response = secrets_client.get_secret_value(SecretId=os.environ["SPIKE_SECRET_ARN"])
        deadline.remaining(reserve=3)
        private = strict_json(secret_response["SecretString"].encode())
        settings = OidcProbeSettings(OIDC_CLIENT_SECRET=private["client_secret"])
        values = settings.model_dump()
        values["client_secret"] = settings.client_secret.get_secret_value()
        provider = CognitoProvider(**values)
        if not re.fullmatch(r"https://[a-z0-9]+\.lambda-url\.ap-southeast-1\.on\.aws", provider.public_origin):
            raise OidcRejected()
        application = SpikeApplication(provider, private["operator_secret"], private["encryption_key"],
                                       DynamoSpikeStore(os.environ["SPIKE_TABLE_NAME"], deadline=deadline), deadline=deadline)
        result = asyncio.run(application.handle(event))
    except StoreRejected:
        result = response(503, {"status": "unavailable"})
    except OidcRejected:
        result = response(400, {"status": "rejected"})
    except Exception:
        result = response(503, {"status": "unavailable"})
    logging.getLogger("enterprise_ai_platform.oidc_spike").info("spike_request status=%s", result["statusCode"])
    return result
