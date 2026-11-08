"""Authorization lifetime is one request/transaction, not a reusable allow token."""

from contextlib import asynccontextmanager
from uuid import UUID
from enterprise_ai_platform.modules.authorization.domain import evaluate, AccessPermit, Action
from enterprise_ai_platform.modules.authorization.ports import AccessFailure
from enterprise_ai_platform.modules.identity.service import digest, opaque


class AuthorizationService:
    def __init__(self, repository, public_origin):
        self.repository, self.public_origin = repository, public_origin

    def token_hash(self, session):
        if not opaque(session):
            raise AccessFailure(401)
        return digest("session", session)

    @asynccontextmanager
    async def access(self, session, ref, action, *, changed_fields=frozenset(), headers=None):
        token_hash = self.token_hash(session)
        csrf_hash = None
        if action != Action.READ:
            self.browser_guard(headers or {})
            csrf_hash = digest("csrf", headers["x-csrf-token"])
        if type(changed_fields) is not frozenset or any(not isinstance(k, str) for k in changed_fields):
            raise AccessFailure(400)
        async with self.repository.scope(token_hash, csrf_hash=csrf_hash) as unit:
            if unit.subject.tenant_id != ref.tenant_id:
                raise AccessFailure(404)
            resource = await unit.resource(ref)
            denied = evaluate(unit.subject, resource, action, changed_fields=changed_fields)
            if denied:
                raise AccessFailure(denied)
            async def apply_write(command):
                await unit.validate()
                current = await unit.resource(ref)
                denied = evaluate(unit.subject, current, action, changed_fields=changed_fields)
                if denied:
                    raise AccessFailure(denied)
                if current != resource:
                    raise AccessFailure(409)
                return await unit.write(resource, action, command)
            permit = AccessPermit(resource, action, apply_write)
            yield permit
            current = await unit.resource(ref)
            if permit.expected_after is None:
                denied = evaluate(unit.subject, current, action, changed_fields=changed_fields)
                if denied:
                    raise AccessFailure(denied)
            # Reads must remain unchanged. A successful mutation supplies its exact
            # stored snapshot; a stage transition can legitimately remove the actor's
            # next-stage rights. Business validation/CAS belongs to the owning feature.
            if current != (permit.expected_after or resource):
                raise AccessFailure(409)

    def browser_guard(self, headers):
        if (headers.get("origin") != self.public_origin or headers.get("content-type") != "application/json"
                or not opaque(headers.get("x-csrf-token"))):
            raise AccessFailure(403)

    async def change_role(self, session, headers, command):
        token_hash = self.token_hash(session)
        self.browser_guard(headers)
        fields = {"operation", "membership_id", "expected_version", "role", "scope_kind", "scope_ref", "idempotency_key"}
        try:
            if (type(command) is not dict or set(command) != fields or command["operation"] not in {"grant", "revoke"}
                    or type(command["expected_version"]) is not int or not 1 <= command["expected_version"] <= 999999999999999999
                    or any(not isinstance(command[k], str) for k in fields - {"expected_version"})
                    or str(UUID(command["membership_id"])) != command["membership_id"]
                    or str(UUID(command["idempotency_key"])) != command["idempotency_key"]
                    or len(command["scope_ref"]) > 200 or len(command["scope_kind"]) > 64
                    or command["role"] not in {"employee", "knowledge_manager", "tenant_admin", "process_owner", "approver"}):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise AccessFailure(400) from None
        return await self.repository.change_role(token_hash, digest("csrf", headers["x-csrf-token"]), dict(command))
