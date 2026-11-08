"""Trusted ports. Resource facts come from current repositories/service checks, never request JSON."""

from typing import Protocol
from enterprise_ai_platform.modules.authorization.domain import Resource, ResourceRef, Subject


class AccessFailure(RuntimeError):
    def __init__(self, status: int):
        self.status = status if status in {400,401,403,404,409,503} else 503
        super().__init__("Access check rejected.")


class AccessUnitPort(Protocol):
    subject: Subject
    async def resource(self, ref: ResourceRef) -> Resource | None: ...
    async def validate(self) -> None: ...
    async def write(self, resource: Resource, action, command) -> Resource: ...


class AuthorizationRepositoryPort(Protocol):
    def scope(self, token_hash: str, *, csrf_hash=None): ...
    async def change_role(self, token_hash: str, csrf_hash: str, command: dict) -> dict: ...


class AuthorizationServicePort(Protocol):
    def access(self, session: str, ref: ResourceRef, action, *, changed_fields=frozenset(), headers=None): ...
    async def change_role(self, session: str, headers: dict, command: dict) -> dict: ...


# Public exports let feature modules depend on policy types through this port.
from enterprise_ai_platform.modules.authorization.domain import Action, ACLGrant, RoleGrant, AccessPermit
