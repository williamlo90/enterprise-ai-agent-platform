"""Managed runtime logins; no privileged identity is ever adopted as runtime."""

from dataclasses import replace
from hashlib import sha256
import re
import secrets

from psycopg import sql

from enterprise_ai_platform.infrastructure.database.connection import (
    ConnectionTarget, RuntimeDatabase, bounded_transaction, direct_connection,
    verify_same_database,
)
from enterprise_ai_platform.infrastructure.database.migration import access_role, migrate

RUNTIME_ROLE = "enterprise_ai_platform_runtime"


class RoleProvisioningError(RuntimeError):
    def __init__(self):
        super().__init__("Runtime role provisioning or verification failed.")


async def provision_role(direct: ConnectionTarget, base: ConnectionTarget, *, schema="platform", role_name=RUNTIME_ROLE, rotate=False):
    verify_same_database(base, direct)
    if (schema == "platform" and role_name != RUNTIME_ROLE) or (
        schema != "platform" and (not re.fullmatch(r"db03_probe_[a-f0-9]{32}", schema)
                                   or role_name != "eai_runtime_" + schema.rsplit("_", 1)[1])):
        raise RoleProvisioningError()
    current, head = await migrate(direct, schema=schema)
    if current != head or head < 3:
        raise RoleProvisioningError()
    runtime = base
    lock = int.from_bytes(sha256(("enterprise-ai:" + schema).encode()).digest()[:8], "big", signed=True)
    async with direct_connection(direct, timeout=30) as connection:
        async with bounded_transaction(connection):
            await connection.execute("SET LOCAL lock_timeout='5s'")
            await connection.execute("SET LOCAL statement_timeout='10s'")
            await connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
            ident, login, group = sql.Identifier(schema), sql.Identifier(role_name), sql.Identifier(access_role(schema))
            cursor = await connection.execute(sql.SQL(
                "SELECT role_name::text,role_oid FROM {}.runtime_role_registry"
            ).format(ident))
            binding = await cursor.fetchone()
            cursor = await connection.execute("SELECT oid FROM pg_catalog.pg_roles WHERE rolname=%s", (role_name,))
            actual = await cursor.fetchone()
            if binding is None:
                if actual is not None:
                    raise RoleProvisioningError()
                await connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOINHERIT CONNECTION LIMIT 8").format(login))
                cursor = await connection.execute("SELECT oid FROM pg_catalog.pg_roles WHERE rolname=%s", (role_name,))
                actual = await cursor.fetchone()
                await connection.execute(sql.SQL("INSERT INTO {}.runtime_role_registry(role_name,role_oid) VALUES (%s,%s)").format(ident),
                                         (role_name, actual[0]))
                await connection.execute(sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET FALSE, ADMIN FALSE").format(group, login))
                await connection.execute(sql.SQL("ALTER ROLE {} SET search_path=pg_catalog").format(login))
                await connection.execute(sql.SQL("ALTER ROLE {} SET row_security=on").format(login))
                cursor = await connection.execute("SELECT current_database()")
                database_name = (await cursor.fetchone())[0]
                await connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database_name), login))
            elif actual is None or binding != (role_name, actual[0]):
                raise RoleProvisioningError()
            # Privilege preflight precedes every credential change. It also
            # validates the access role through this login's effective rights.
            cursor = await connection.execute(sql.SQL("SELECT {}._runtime_role_is_valid(%s)").format(ident), (actual[0],))
            if await cursor.fetchone() != (True,):
                raise RoleProvisioningError()
            if binding is not None and not rotate:
                if base.user != role_name:
                    # Recovery after a failed local save requires --rotate;
                    # normal reruns never invalidate a working password.
                    raise RoleProvisioningError()
            else:
                password = secrets.token_urlsafe(48)
                # Neon requires plaintext password input over verified TLS.
                # Driver logs/CLI never expose the statement or its parameter.
                await connection.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(login, sql.Literal(password)))
                runtime = replace(base, user=role_name, password=password)
    database = RuntimeDatabase(runtime, schema=schema)
    try:
        await database.open()  # Actual login, not SET ROLE from an owner session.
    finally:
        await database.close()
    return runtime


async def drop_probe_schema(direct: ConnectionTarget, schema: str):
    """Only drop roles whose OIDs are recorded in this run's disposable schema."""
    if not re.fullmatch(r"db0[23]_probe_[a-f0-9]{32}", schema):
        raise RoleProvisioningError()
    async with direct_connection(direct, timeout=30) as connection:
        roles = []
        cursor = await connection.execute("SELECT to_regclass(%s)", (schema + ".runtime_access_role_registry",))
        if (await cursor.fetchone())[0] is not None:
            for table in ("runtime_role_registry", "runtime_access_role_registry"):
                cursor = await connection.execute(sql.SQL("SELECT b.role_name::text,b.role_oid FROM {}.{} b "
                    "JOIN pg_catalog.pg_roles r ON r.oid=b.role_oid AND r.rolname=b.role_name").format(
                        sql.Identifier(schema), sql.Identifier(table)))
                roles.extend(await cursor.fetchall())
        await connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        for name, oid in roles:
            expected = {access_role(schema), "eai_runtime_" + schema.rsplit("_", 1)[1]}
            if name not in expected:
                raise RoleProvisioningError()
            cursor = await connection.execute("SELECT oid FROM pg_catalog.pg_roles WHERE rolname=%s", (name,))
            if await cursor.fetchone() != (oid,):
                raise RoleProvisioningError()
            # CONNECT is a separate database grant and survives DROP SCHEMA.
            if name.startswith("eai_runtime_"):
                cursor = await connection.execute("SELECT current_database()")
                dbname = (await cursor.fetchone())[0]
                await connection.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                    sql.Identifier(dbname), sql.Identifier(name)))
            await connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
