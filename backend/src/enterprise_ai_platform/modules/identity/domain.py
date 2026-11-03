"""Trusted context identifiers and identity protocol validation rules."""

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import hmac
import re
from urllib.parse import urlsplit
from uuid import UUID
import time


@dataclass(frozen=True, repr=False)
class TenantActorContext:
    tenant_id: UUID
    actor_id: UUID

    def __post_init__(self):
        if not isinstance(self.tenant_id, UUID) or not isinstance(self.actor_id, UUID):
            raise ValueError("Database context requires UUID identifiers.")

    def __repr__(self):
        return "TenantActorContext(<redacted>)"


class OidcRejected(ValueError):
    def __init__(self):
        super().__init__("OIDC validation rejected.")


@dataclass(frozen=True, repr=False)
class CognitoProvider:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    public_origin: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str

    def __repr__(self):
        return "CognitoProvider(<redacted>)"

    def __post_init__(self):
        issuer = re.fullmatch(
            r"https://cognito-idp\.(ap-southeast-1)\.amazonaws\.com/(ap-southeast-1_[A-Za-z0-9]+)",
            self.issuer,
        )
        origin = urlsplit(self.public_origin)
        authorization = urlsplit(self.authorization_endpoint)
        if (not issuer or not self.client_id or len(self.client_id.encode()) > 256
                or not self.client_secret or len(self.client_secret) > 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in self.client_secret)
                or origin.scheme != "https" or not origin.hostname
                or origin.username or origin.password or origin.port
                or origin.path or origin.query or origin.fragment
                or self.redirect_uri != self.public_origin + "/api/v1/auth/callback"
                or authorization.scheme != "https" or authorization.username
                or authorization.password or authorization.port
                or not re.fullmatch(r"[a-z0-9-]+\.auth\.ap-southeast-1\.amazoncognito\.com",
                                    authorization.hostname or "")
                or authorization.path != "/oauth2/authorize"
                or authorization.query or authorization.fragment
                or self.token_endpoint != self.authorization_endpoint.removesuffix("authorize") + "token"
                or self.jwks_uri != self.issuer + "/.well-known/jwks.json"):
            raise OidcRejected()


def nonce_digest(value: str) -> str:
    return hashlib.sha256(("eai:oidc-nonce:v1:" + value).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class ProbeDeadline:
    end: float
    clock: Callable[[], float] = time.monotonic

    def remaining(self, reserve: float = 0) -> float:
        value = self.end - self.clock() - reserve
        if value <= 0:
            raise TimeoutError("Probe deadline exceeded.")
        return value


def validate_claims(claims: dict, provider: CognitoProvider, expected_nonce_hash: str, now: int):
    """Only call after trusted-key signature verification; returns no identity/PII."""
    try:
        if not isinstance(claims, dict) or claims.get("iss") != provider.issuer:
            raise OidcRejected()
        subject = claims.get("sub")
        if not isinstance(subject, str) or not 1 <= len(subject.encode("utf-8")) <= 512:
            raise OidcRejected()
        if claims.get("token_use") != "id":
            raise OidcRejected()
        audience = claims.get("aud")
        audience = [audience] if isinstance(audience, str) else audience
        if (not isinstance(audience, list) or not audience
                or any(not isinstance(item, str) or not item for item in audience)
                or len(set(audience)) != len(audience) or provider.client_id not in audience):
            raise OidcRejected()
        if ((len(audience) > 1 or "azp" in claims) and claims.get("azp") != provider.client_id):
            raise OidcRejected()
        expiry, issued = claims.get("exp"), claims.get("iat")
        if (type(now) is not int or type(expiry) is not int or type(issued) is not int
                or expiry <= issued or now >= expiry + 60
                or issued > now + 60 or issued < now - 660):
            raise OidcRejected()
        if "nbf" in claims and (type(claims["nbf"]) is not int or claims["nbf"] > now + 60):
            raise OidcRejected()
        nonce = claims.get("nonce")
        if (not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", nonce)
                or not re.fullmatch(r"[a-f0-9]{64}", expected_nonce_hash)
                or not hmac.compare_digest(nonce_digest(nonce), expected_nonce_hash)):
            raise OidcRejected()
    except (TypeError, UnicodeError, OverflowError):
        raise OidcRejected() from None
