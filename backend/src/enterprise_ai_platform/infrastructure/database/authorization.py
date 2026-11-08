"""Session-bound access units and narrow role changes through the restricted DB login."""

from contextlib import asynccontextmanager
from uuid import UUID
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.authorization.domain import Subject, RoleGrant
from enterprise_ai_platform.modules.authorization.ports import AccessFailure


def rejected(result):
    status = result.get("status")
    return {"unauthenticated": 401, "forbidden": 403, "not_found": 404, "conflict": 409, "invalid": 400}.get(status, 503)


def subject_from(result):
    identity = result["identity"]
    tenant, member = identity["active_tenant_id"], identity["active_membership_id"]
    selected = [m for m in identity["memberships"] if m["membership_id"] == member and m["tenant_id"] == tenant]
    if len(selected) != 1:
        raise AccessFailure(503)
    return Subject(UUID(tenant), UUID(identity["principal_id"]), UUID(member), identity["access_revision"],
                   tuple(RoleGrant(**g) for g in result["grants"]))


class AccessUnit:
    def __init__(self, connection, subject, resource_loader, resource_writer, validate):
        self.connection, self.subject, self.resource_loader = connection, subject, resource_loader
        self.resource_writer, self._validate = resource_writer, validate
        self.closed = False

    async def validate(self):
        if self.closed:
            raise AccessFailure(503)
        await self._validate()

    async def write(self, resource, action, command):
        await self.validate()
        if self.resource_writer is None:
            raise AccessFailure(503)
        return await self.resource_writer(self.connection, self.subject, resource, action, command)

    async def resource(self, ref):
        if self.closed:
            raise AccessFailure(503)
        if ref.tenant_id != self.subject.tenant_id:
            return None
        if self.resource_loader is None:
            # A future feature must bind its authoritative repository/service loader.
            raise AccessFailure(503)
        result = await self.resource_loader(self.connection, self.subject, ref)
        if result is not None and result.ref != ref:
            raise AccessFailure(503)
        return result


class PostgresAuthorizationRepository:
    def __init__(self, database, *, resource_loader=None, resource_writer=None):
        self.database, self.resource_loader, self.resource_writer = database, resource_loader, resource_writer

    async def _call(self, connection, token_hash, operation, payload):
        result = await connection.execute(sql.SQL("SELECT {}.authorization_call(%s,%s,%s)").format(
            sql.Identifier(self.database.schema)), (token_hash, operation, Jsonb(payload)))
        return (await result.fetchone())[0]

    @asynccontextmanager
    async def scope(self, token_hash, *, csrf_hash=None):
        unit = None
        initial = None
        try:
            async with self.database.transaction() as connection:
                payload = {} if csrf_hash is None else {"csrf_hash":csrf_hash}
                initial = await self._call(connection, token_hash, "context", payload)
                if initial.get("status") == "ok":
                    subject = subject_from(initial)
                    await connection.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.actor_id',%s,true)",
                                             (str(subject.tenant_id), str(subject.principal_id)))
                    async def validate():
                        current = await self._call(connection, token_hash, "context", payload)
                        if current.get("status") != "ok":
                            raise AccessFailure(rejected(current))
                        if subject_from(current) != subject:
                            raise AccessFailure(409)
                    unit = AccessUnit(connection, subject, self.resource_loader, self.resource_writer, validate)
                    try:
                        yield unit
                        final = await self._call(connection, token_hash, "finish", payload)
                        if final.get("status") != "ok":
                            raise AccessFailure(rejected(final))
                        if subject_from(final) != unit.subject:
                            raise AccessFailure(409)
                    finally:
                        unit.closed = True
            # Commit revocation on a stale initial session, without touching activity.
            if initial.get("status") != "ok":
                raise AccessFailure(rejected(initial))
        except AccessFailure:
            raise
        except Exception:
            raise AccessFailure(503) from None

    async def change_role(self, token_hash, csrf_hash, command):
        try:
            async with self.database.transaction() as connection:
                result = await self._call(connection, token_hash, command["operation"], {**command, "csrf_hash": csrf_hash})
            if result.get("status") != "ok":
                raise AccessFailure(rejected(result))
            return {key: value for key, value in result.items() if key != "status"}
        except AccessFailure:
            raise
        except Exception:
            raise AccessFailure(503) from None
