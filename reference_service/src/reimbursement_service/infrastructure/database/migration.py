"""Explicit operator migration; owned schema/login and checksum history only."""
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import re
import psycopg
from psycopg import sql
from reimbursement_service.infrastructure.database.connection import bounded_transaction, direct_connection
from reimbursement_service.infrastructure.database.target import check_pair, schema_and_role
from reimbursement_service.modules.system.domain import DatabaseUnavailable


class MigrationError(RuntimeError):
    def __init__(self):
        super().__init__("Service migration state or operation is invalid.")


@dataclass(frozen=True)
class Revision:
    version: int
    name: str
    source: str

    @property
    def checksum(self):
        return sha256(self.source.encode("utf-8")).hexdigest()


def revisions():
    assets = files("reimbursement_service.infrastructure.database").joinpath("migrations")
    result = []
    for item in sorted(assets.iterdir(), key=lambda item: item.name):
        if not item.name.endswith(".sql"):
            continue
        match = re.fullmatch(r"([0-9]{4})_[a-z][a-z0-9_]*\.sql", item.name)
        if not match:
            raise MigrationError()
        result.append(Revision(int(match[1]), item.name, item.read_text(encoding="utf-8").replace("\r\n", "\n")))
    if not result or [r.version for r in result] != list(range(1, len(result) + 1)):
        raise MigrationError()
    return tuple(result)


def validate_history(history, manifest):
    if list(history) != [(r.version, r.name, r.checksum) for r in manifest[:len(history)]]:
        raise MigrationError()


async def read_history(connection, schema):
    ns = await (await connection.execute(
        "SELECT n.oid, n.nspowner=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user) "
        "FROM pg_catalog.pg_namespace n WHERE n.nspname=%s", (schema,))).fetchone()
    if ns is None:
        return None
    if not ns[1]:
        raise MigrationError()
    cursor = await connection.execute(sql.SQL(
        "SELECT version,name,checksum FROM {}.schema_migrations ORDER BY version"
    ).format(sql.Identifier(schema)))
    history = await cursor.fetchall()
    if not history:
        raise MigrationError()
    return history


async def migrate(migration_target, runtime_target, *, schema="reimbursement", upgrade=False,target_version=None):
    check_pair(migration_target, runtime_target, schema)
    manifest = revisions()
    head=len(manifest) if target_version is None else target_version
    if type(head) is not int or not 0<=head<=len(manifest):raise MigrationError()
    runtime = schema_and_role(schema)
    lock_key = int.from_bytes(sha256(("reimbursement:" + schema).encode()).digest()[:8], "big", signed=True)
    try:
        async with direct_connection(migration_target, timeout=45) as connection:
            async with bounded_transaction(connection):
                await connection.execute("SET LOCAL search_path=pg_catalog")
                await connection.execute("SET LOCAL statement_timeout='15s'")
                await connection.execute("SET LOCAL lock_timeout='3s'")
                if upgrade:
                    await connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
                else:
                    await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                history = await read_history(connection, schema)
                if history is not None:
                    validate_history(history, manifest)
                    binding = await (await connection.execute(sql.SQL(
                        "SELECT b.role_name,r.rolname FROM {}.runtime_binding b "
                        "JOIN pg_catalog.pg_roles r ON r.oid=b.role_oid WHERE b.singleton"
                    ).format(sql.Identifier(schema)))).fetchall()
                    if binding != [(runtime, runtime)]:
                        raise MigrationError()
                elif await (await connection.execute(
                        "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=%s", (runtime,))).fetchone():
                    raise MigrationError()  # Never adopt an existing unrelated login.
                current = len(history or [])
                if head<current:raise MigrationError()
                if not upgrade:
                    return current, head
                if current==head:return current,head
                if history is None:
                    if len(runtime_target.password) < 32:
                        raise MigrationError()
                    await connection.execute(sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOINHERIT CONNECTION LIMIT 4 PASSWORD {}"
                    ).format(sql.Identifier(runtime), sql.Literal(runtime_target.password)))
                    await connection.execute(sql.SQL("ALTER ROLE {} SET search_path=pg_catalog").format(sql.Identifier(runtime)))
                    await connection.execute(sql.SQL("ALTER ROLE {} SET row_security=on").format(sql.Identifier(runtime)))
                    dbname = (await (await connection.execute("SELECT current_database()")).fetchone())[0]
                    await connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(dbname), sql.Identifier(runtime)))
                    await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                    await connection.execute(sql.SQL(
                        "CREATE TABLE {}.schema_migrations (version integer PRIMARY KEY CHECK(version>0), "
                        "name text NOT NULL UNIQUE, checksum text NOT NULL CHECK(checksum ~ '^[a-f0-9]{{64}}$'), "
                        "applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                    ).format(sql.Identifier(schema)))
                for item in manifest[current:head]:
                    await connection.execute(sql.SQL(item.source).format(
                        schema=sql.Identifier(schema), schema_name=sql.Literal(schema),
                        runtime=sql.Identifier(runtime), runtime_name=sql.Literal(runtime)), prepare=False)
                    await connection.execute(sql.SQL(
                        "INSERT INTO {}.schema_migrations(version,name,checksum) VALUES (%s,%s,%s)"
                    ).format(sql.Identifier(schema)), (item.version, item.name, item.checksum))
                return head,head
    except (psycopg.Error, DatabaseUnavailable, TimeoutError):
        raise MigrationError() from None
