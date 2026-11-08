"""Opaque sessions, bounded OIDC transactions and same-origin browser actions."""

import base64
import hashlib
import ipaddress
import re
import secrets
from urllib.parse import parse_qsl, urlencode
from uuid import UUID, uuid4

from .domain import CognitoProvider, TenantActorContext, nonce_digest
from .ports import IdentityFailure, IdentityProvider, IdentityRepository, VerifierProtector, BrowserContextFailure


def opaque(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is not None


def digest(kind: str, value: str) -> str:
    return hashlib.sha256(("eai:" + kind + ":v1:" + value).encode("ascii")).hexdigest()


class IdentityService:
    def __init__(self, provider: CognitoProvider, repository: IdentityRepository,
                 oidc: IdentityProvider, protector: VerifierProtector):
        self.provider, self.repository, self.oidc, self.protector = provider, repository, oidc, protector

    async def _call(self, operation, payload):
        try:
            result = await self.repository.call(operation, payload)
        except BrowserContextFailure:
            raise
        except Exception:
            raise IdentityFailure(503) from None
        status = result.get("status")
        if status != "ok":
            raise IdentityFailure({"unauthenticated": 401, "forbidden": 403, "rate_limited": 429}.get(status, 503),
                                  retry_after=result.get("retry_after", 0))
        return result

    async def _limit(self, endpoint, binding, ip):
        try:
            source = str(ipaddress.ip_address(ip))
        except ValueError:
            raise IdentityFailure(503) from None
        keys = [endpoint + ":ip:" + digest("ip", source)]
        if opaque(binding):
            keys.append(endpoint + ":binding:" + digest("binding", binding))
        await self._call("limit", {"keys": keys})

    async def login(self, return_to, old_binding, ip):
        await self._limit("login", old_binding, ip)
        if return_to not in {"home", "tenant_picker"}:
            raise IdentityFailure(400)
        binding, state, nonce, verifier = [secrets.token_urlsafe(32) for _ in range(4)]
        await self._call("login_start", {
            "id": str(uuid4()), "binding_hash": digest("binding", binding), "state_hash": digest("state", state),
            "nonce_hash": nonce_digest(nonce), "verifier": self.protector.protect(verifier),
            "old_binding": digest("binding", old_binding) if opaque(old_binding) else None,
            "issuer": self.provider.issuer, "client_id": self.provider.client_id,
            "redirect_uri": self.provider.redirect_uri, "return_to": return_to,
        })
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return {"binding": binding, "location": self.provider.authorization_endpoint + "?" + urlencode({
            "response_type": "code", "client_id": self.provider.client_id, "redirect_uri": self.provider.redirect_uri,
            "scope": "openid profile", "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })}

    async def callback(self, raw_query, binding, old_session, ip):
        await self._limit("callback", binding, ip)
        try:
            if len(raw_query) > 8192 or re.search(rb"%(?![a-fA-F0-9]{2})", raw_query):
                raise ValueError
            pairs = parse_qsl(raw_query.decode("ascii"), keep_blank_values=True, max_num_fields=10,
                              encoding="utf-8", errors="strict")
            values = dict(pairs)
            if (len(values) != len(pairs) or set(values) - {"code", "state", "iss", "error", "error_description"}
                    or not opaque(values.get("state")) or ("code" in values) == ("error" in values)
                    or ("code" in values and not 1 <= len(values["code"].encode()) <= 4096)):
                raise ValueError
        except (ValueError, UnicodeError):
            raise IdentityFailure(400) from None
        if "iss" in values and values["iss"] != self.provider.issuer:
            raise IdentityFailure(401)
        if not opaque(binding):
            raise IdentityFailure(401)
        claim = await self._call("login_claim", {"binding_hash": digest("binding", binding),
                                               "state_hash": digest("state", values["state"])})
        if "error" in values:
            raise IdentityFailure(401)
        if (claim["issuer"], claim["client_id"], claim["redirect_uri"]) != (
                self.provider.issuer, self.provider.client_id, self.provider.redirect_uri):
            raise IdentityFailure(401)
        try:
            verifier = self.protector.reveal(claim["verifier"])
            identity = await self.oidc.exchange(values["code"], verifier, claim["nonce_hash"])
        except IdentityFailure:
            raise
        except Exception:
            raise IdentityFailure(401) from None
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        completed = await self._call("login_complete", {
            "transaction_id": claim["id"], "issuer": self.provider.issuer, "subject": identity["subject"],
            "principal_id": str(uuid4()), "session_id": str(uuid4()),
            "token_hash": digest("session", token), "csrf_hash": digest("csrf", csrf),
            "old_session_hash": digest("session", old_session) if opaque(old_session) else None,
        })
        return {"token": token, "max_age": completed["max_age"],
                "location": {"home": "/", "tenant_picker": "/tenants"}[claim["return_to"]]}

    def _session_hash(self, session):
        if not opaque(session):
            raise IdentityFailure(401)
        return digest("session", session)

    def _browser_guard(self, headers, *, bootstrap=False):
        origin = headers.get("origin")
        if bootstrap:
            valid = (headers.get("x-eai-csrf-bootstrap") == "1" and headers.get("sec-fetch-site") == "same-origin"
                     and (origin is None or origin == self.provider.public_origin))
        else:
            valid = (origin == self.provider.public_origin and headers.get("content-type") == "application/json"
                     and opaque(headers.get("x-csrf-token")))
        if not valid:
            raise IdentityFailure(403)

    async def me(self, session, headers):
        token_hash = self._session_hash(session)
        self._browser_guard(headers, bootstrap=True)
        csrf = secrets.token_urlsafe(32)
        result = await self._call("me", {"token_hash": token_hash, "new_csrf_hash": digest("csrf", csrf)})
        return {**result["identity"], "csrf_token": csrf}

    async def logout(self, session, headers):
        token_hash = self._session_hash(session)
        self._browser_guard(headers)
        await self._call("logout", {"token_hash": token_hash, "csrf_hash": digest("csrf", headers["x-csrf-token"])})

    async def switch(self, session, headers, membership_id):
        token_hash = self._session_hash(session)
        self._browser_guard(headers)
        try:
            membership = str(UUID(membership_id))
        except (ValueError, TypeError, AttributeError):
            raise IdentityFailure(400) from None
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        result = await self._call("switch", {"token_hash": token_hash, "csrf_hash": digest("csrf", headers["x-csrf-token"]),
            "membership_id": membership, "session_id": str(uuid4()),
            "new_token_hash": digest("session", token), "new_csrf_hash": digest("csrf", csrf)})
        return {"token": token, "max_age": result["max_age"], "body": {**result["identity"], "csrf_token": csrf}}

    async def resolve(self, session):
        result = await self._call("resolve", {"token_hash": self._session_hash(session)})
        return TenantActorContext(UUID(result["identity"]["active_tenant_id"]), UUID(result["identity"]["principal_id"]))
