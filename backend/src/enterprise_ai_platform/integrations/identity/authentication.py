"""Cognito exchange with durable JWKS leases, bounded cache and secret protection."""

import asyncio
import base64
import hashlib
import secrets
import time

from cryptography.fernet import Fernet

from enterprise_ai_platform.modules.identity.ports import IdentityFailure
from enterprise_ai_platform.integrations.identity.cognito_probe import (
    CognitoProbe, strict_json, validate_keys, verify_id_token,
)


class FernetVerifierProtector:
    def __init__(self, key: str):
        self.cipher = Fernet(key.encode("ascii"))

    def protect(self, value):
        return self.cipher.encrypt(value.encode("ascii")).decode("ascii")

    def reveal(self, value):
        return self.cipher.decrypt(value.encode("ascii"), ttl=300).decode("ascii")


class CognitoAuthentication:
    def __init__(self, provider, repository, *, transport=None, clock=time.time, cache_clock=time.time):
        self.provider, self.repository, self.clock, self.cache_clock = provider, repository, clock, cache_clock
        self.http = CognitoProbe(provider, transport=transport)

    async def _cache(self, operation, payload):
        try:
            result = await self.repository.call(operation, {"issuer": self.provider.issuer, **payload})
            if result.get("status") != "ok":
                raise ValueError
            return result
        except Exception:
            raise IdentityFailure(503) from None

    async def keys(self, kid, *, scheduled=False):
        lease_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        payload = {"kid": kid, "lease_hash": lease_hash, "scheduled": scheduled}
        async with asyncio.timeout(6):
            snapshot = await self._cache("jwks_reserve", payload)
            if snapshot["refresh"]:
                try:
                    status, document = await self.http.request("GET", self.provider.jwks_uri)
                    if status != 200:
                        raise ValueError
                    keys = validate_keys(document)
                    await self._cache("jwks_publish", {**payload, "keys": keys})
                    snapshot = await self._cache("jwks_reserve", payload)
                except Exception:
                    # Release only our exact lease. A failed refresh never replaces keys.
                    try:
                        await self._cache("jwks_failure", payload)
                    except IdentityFailure:
                        pass
                    snapshot = await self._cache("jwks_reserve", payload)
                    if not any(k.get("kid") == kid for k in snapshot["keys"]):
                        raise IdentityFailure(503) from None
            elif snapshot.get("inflight") and not any(k.get("kid") == kid for k in snapshot["keys"]):
                # Single-flight across instances, with a bounded wait for the owner.
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    snapshot = await self._cache("jwks_reserve", payload)
                    if not snapshot.get("inflight"):
                        break
            if snapshot.get("valid_until") is None or self.cache_clock() >= float(snapshot["valid_until"]):
                return []
            return snapshot["keys"]

    async def refresh(self):
        await self.keys("", scheduled=True)

    async def exchange(self, code, verifier, nonce_hash):
        try:
            status, tokens = await self.http.request("POST", self.provider.token_endpoint, basic=True, form={
                "grant_type": "authorization_code", "code": code,
                "code_verifier": verifier, "redirect_uri": self.provider.redirect_uri,
            })
        except Exception:
            raise IdentityFailure(503) from None
        if status != 200 or not isinstance(tokens, dict):
            raise IdentityFailure(401)
        token = tokens.get("id_token")
        try:
            if not isinstance(token, str) or len(token.encode()) > 16384:
                raise ValueError
            parts = token.split(".")
            if len(parts) != 3:
                raise ValueError
            header = strict_json(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
            kid = header.get("kid")
            if (header.get("alg") != "RS256" or not isinstance(kid, str) or not 1 <= len(kid.encode()) <= 128
                    or any(k in header for k in ("jku", "x5u", "jwk", "crit", "zip", "b64"))):
                raise ValueError
        except Exception:
            raise IdentityFailure(401) from None
        try:
            keys = await self.keys(kid)
        except (IdentityFailure, TimeoutError):
            raise IdentityFailure(503) from None
        try:
            verify_id_token(token, keys, self.provider, nonce_hash, int(self.clock()))
            claims = strict_json(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            return {"subject": claims["sub"]}
        except Exception:
            raise IdentityFailure(401) from None
