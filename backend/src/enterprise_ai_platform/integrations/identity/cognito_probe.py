"""Bounded provider feasibility probes, not application authentication endpoints."""

import asyncio
import base64
import json
import logging
import secrets
from urllib.parse import quote

import httpx
import jwt

from enterprise_ai_platform.modules.identity.domain import (
    CognitoProvider, OidcRejected, validate_claims,
)


class ProbeUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("OIDC provider probe unavailable.")


class SafeOidcLog(logging.Filter):
    def filter(self, record):
        record.msg, record.args = "OIDC transport event.", ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


def safe_transport_logs():
    # New SDK child loggers may be created after client construction. Ancestor
    # filters alone do not sanitize them; chain the record factory once too.
    current=logging.getLogRecordFactory()
    if not getattr(current,"_eai_transport_redaction",False):
        def redacted_record(*args,**kwargs):
            record=current(*args,**kwargs)
            if record.name.split(".",1)[0] in {"boto3","botocore","httpx","httpcore","psycopg","psycopg_pool"}:
                record.msg,record.args="Transport event.",()
                record.exc_info=record.exc_text=record.stack_info=None
            return record
        redacted_record._eai_transport_redaction=True
        logging.setLogRecordFactory(redacted_record)
    for name in {"boto3", "botocore", "httpx", "httpcore", "httpcore.connection", "httpcore.http11",
                 "httpcore.http2", "httpcore.proxy", "httpcore.socks"} | {
        name for name in logging.Logger.manager.loggerDict if name.startswith(("httpx.", "httpcore.", "botocore.", "boto3."))
    }:
        logger = logging.getLogger(name)
        if not any(isinstance(item, SafeOidcLog) for item in logger.filters):
            logger.addFilter(SafeOidcLog())


def strict_json(value: bytes):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise OidcRejected()
            result[key] = item
        return result
    try:
        return json.loads(value.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(OidcRejected()))
    except (ValueError, UnicodeError, RecursionError):
        raise OidcRejected() from None


def validate_keys(document):
    try:
        keys = document["keys"]
        if not isinstance(keys, list) or not 1 <= len(keys) <= 32:
            raise OidcRejected()
        identifiers = set()
        for key in keys:
            if (not isinstance(key, dict) or key.get("kty") != "RSA"
                    or key.get("alg") != "RS256" or key.get("use", "sig") != "sig"
                    or key.get("key_ops", ["verify"]) != ["verify"]
                    or not isinstance(key.get("kid"), str) or not 1 <= len(key["kid"].encode()) <= 128
                    or key["kid"] in identifiers or "d" in key):
                raise OidcRejected()
            public = jwt.PyJWK.from_dict(key, algorithm="RS256").key
            if not 2048 <= public.key_size <= 8192 or public.public_numbers().e < 3:
                raise OidcRejected()
            identifiers.add(key["kid"])
        return keys
    except Exception:
        raise OidcRejected() from None


def verify_id_token(token: str, keys: list, provider: CognitoProvider,
                    expected_nonce_hash: str, now: int):
    try:
        if not isinstance(token, str) or len(token.encode()) > 16384:
            raise OidcRejected()
        parts = token.split(".")
        if len(parts) != 3:
            raise OidcRejected()
        header, claims = [strict_json(base64.urlsafe_b64decode(item + "=" * (-len(item) % 4)))
                          for item in parts[:2]]
        if (not isinstance(header, dict) or header.get("alg") != "RS256"
                or any(item in header for item in ("jku", "x5u", "jwk", "crit", "zip", "b64"))):
            raise OidcRejected()
        validated = validate_keys({"keys": keys})
        matching = [key for key in validated if key["kid"] == header.get("kid")]
        if len(matching) != 1:
            raise OidcRejected()
        key = jwt.PyJWK.from_dict(matching[0], algorithm="RS256")
        # Parse strict JSON above; PyJWT performs the cryptographic verification.
        jwt.decode(token, key.key, algorithms=["RS256"], options={
            "verify_exp": False, "verify_iat": False, "verify_nbf": False,
            "verify_aud": False, "verify_iss": False, "verify_sub": False,
            "verify_jti": False,
        })
        validate_claims(claims, provider, expected_nonce_hash, now)
    except Exception:
        raise OidcRejected() from None


class CognitoProbe:
    def __init__(self, provider: CognitoProvider, *, transport=None, deadline=None):
        self.provider, self.transport, self.deadline = provider, transport, deadline

    async def request(self, method: str, url: str, *, form=None, basic=False):
        allowed = {self.provider.issuer + "/.well-known/openid-configuration",
                   self.provider.jwks_uri, self.provider.token_endpoint}
        if url not in allowed or (basic and (url != self.provider.token_endpoint or method != "POST")):
            raise ProbeUnavailable()
        safe_transport_logs()
        headers = {"Accept": "application/json"}
        if basic:
            credentials = quote(self.provider.client_id, safe="") + ":" + quote(self.provider.client_secret, safe="")
            headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode("ascii")
        try:
            seconds = min(10 if method == "POST" else 5,
                          self.deadline.remaining(reserve=3) if self.deadline else 10)
            async with asyncio.timeout(seconds):
                async with httpx.AsyncClient(
                    timeout=min(5, seconds), follow_redirects=False, trust_env=False,
                    transport=self.transport, limits=httpx.Limits(max_connections=2),
                ) as client:
                    async with client.stream(method, url, data=form, headers=headers) as response:
                        if response.status_code not in (200, 400, 401):
                            raise ProbeUnavailable()
                        limit = 65536 if method == "POST" else 262144
                        content = bytearray()
                        async for chunk in response.aiter_bytes(8192):
                            content.extend(chunk)
                            if len(content) > limit:
                                raise ProbeUnavailable()
                        return response.status_code, strict_json(bytes(content))
        except Exception:
            raise ProbeUnavailable() from None

    async def preflight(self, *, check_client=False):
        try:
            status, metadata = await self.request(
                "GET", self.provider.issuer + "/.well-known/openid-configuration")
            expected = {"issuer": self.provider.issuer,
                        "authorization_endpoint": self.provider.authorization_endpoint,
                        "token_endpoint": self.provider.token_endpoint, "jwks_uri": self.provider.jwks_uri}
            if (status != 200 or not isinstance(metadata, dict)
                    or any(metadata.get(key) != value for key, value in expected.items())
                    or "code" not in metadata.get("response_types_supported", [])
                    or "RS256" not in metadata.get("id_token_signing_alg_values_supported", [])):
                raise ProbeUnavailable()
            status, document = await self.request("GET", self.provider.jwks_uri)
            if status != 200:
                raise ProbeUnavailable()
            validate_keys(document)
            checks = {"discovery": True, "configured_endpoints": True, "rsa_rs256_jwks": True}
            if check_client:
                # No user authorization code, account, identity or session is involved.
                status, result = await self.request("POST", self.provider.token_endpoint, basic=True, form={
                    "grant_type": "authorization_code", "code": "synthetic-invalid-" + secrets.token_urlsafe(32),
                    "redirect_uri": self.provider.redirect_uri, "code_verifier": secrets.token_urlsafe(32),
                })
                if status != 400 or not isinstance(result, dict) or result.get("error") != "invalid_grant":
                    raise ProbeUnavailable()
                checks["invalid_code_returns_invalid_grant"] = True
            return checks
        except Exception:
            raise ProbeUnavailable() from None
