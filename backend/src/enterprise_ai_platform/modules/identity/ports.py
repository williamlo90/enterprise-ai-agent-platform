"""Identity application boundaries; implementations live in adapters."""

from typing import Protocol
from contextvars import ContextVar
import asyncio
import time

from .domain import CognitoProvider, TenantActorContext


class IdentityFailure(RuntimeError):
    def __init__(self, status: int, *, retry_after: int = 0):
        super().__init__("Identity request rejected.")
        self.status, self.retry_after = status, retry_after


class IdentityRepository(Protocol):
    async def call(self, operation: str, payload: dict) -> dict: ...


class IdentityProvider(Protocol):
    async def exchange(self, code: str, verifier: str, nonce_hash: str) -> dict: ...


class VerifierProtector(Protocol):
    def protect(self, value: str) -> str: ...
    def reveal(self, value: str) -> str: ...


class IdentityServicePort(Protocol):
    provider: CognitoProvider
    async def login(self, return_to: str, old_binding: str | None, ip: str) -> dict: ...
    async def callback(self, raw_query: bytes, binding: str | None, old_session: str | None, ip: str) -> dict: ...
    async def me(self, session: str | None, headers: dict) -> dict: ...
    async def logout(self, session: str | None, headers: dict) -> None: ...
    async def switch(self, session: str | None, headers: dict, membership_id: str) -> dict: ...
    async def resolve(self, session: str | None) -> TenantActorContext: ...


class BrowserContextFailure(RuntimeError):
    def __init__(self, status=503):
        super().__init__('Browser context rejected.')
        self.status = status


class BrowserReadScope:
    """Request-local transaction attestation port; never a reusable authorization grant."""
    def __init__(self, mode, token_hash, *, expected=None, family=None, final_operation=None):
        self.mode, self.token_hash, self.expected = mode, token_hash, expected
        self.family, self.final_operation = family, final_operation
        self.owner = asyncio.current_task()
        self.active = True
        self.attestation = None

    def check_owner(self):
        if not self.active or asyncio.current_task() is not self.owner:
            raise BrowserContextFailure()

    def before(self, token_hash, family):
        self.check_owner()
        self.attestation = None
        if self.mode != 'read' or token_hash != self.token_hash or family != self.family:
            raise BrowserContextFailure()

    def committed(self, token_hash, family, operation, result):
        self.check_owner()
        if token_hash != self.token_hash or family != self.family:
            raise BrowserContextFailure()
        if operation == self.final_operation:
            self.attestation = result

    def response_binding(self):
        self.check_owner()
        result = self.attestation
        if not isinstance(result, dict):
            raise BrowserContextFailure()
        if time.time() >= result['authority_expires_at']:
            raise BrowserContextFailure(401)
        if self.mode == 'read' and result['context_binding'] != self.expected:
            raise BrowserContextFailure(409)
        return result['context_binding']


browser_read_scope: ContextVar[BrowserReadScope | None] = ContextVar('browser_read_scope', default=None)
