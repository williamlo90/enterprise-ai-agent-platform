"""Forward-only, transactional SQL migration runner with a verified history."""

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import re

import psycopg
from psycopg import sql

from enterprise_ai_platform.infrastructure.database.connection import ConnectionTarget, DatabaseUnavailable, direct_connection, bounded_transaction

DEFAULT_SCHEMA = "platform"


class MigrationError(RuntimeError):
    def __init__(self):
        super().__init__("Migration state or operation is invalid.")


@dataclass(frozen=True)
class Revision:
    version: int
    name: str
    source: str

    @property
    def checksum(self):
        return sha256(self.source.encode("utf-8")).hexdigest()


def revisions() -> tuple[Revision, ...]:
    assets = files("enterprise_ai_platform.infrastructure.database").joinpath("migrations")
    result = []
    for item in sorted(assets.iterdir(), key=lambda item: item.name):
        if not item.name.endswith(".sql"):
            continue
        match = re.fullmatch(r"([0-9]{4})_([a-z][a-z0-9_]+)\.sql", item.name)
        if not match:
            raise MigrationError()
        source = item.read_text(encoding="utf-8").replace("\r\n", "\n")
        result.append(Revision(int(match[1]), item.name, source))
    validate_revisions(tuple(result))
    return tuple(result)


def validate_revisions(items: tuple[Revision, ...]):
    if not items or [item.version for item in items] != list(range(1, len(items) + 1)):
        raise MigrationError()
    if len({item.name for item in items}) != len(items) or any(not item.source.strip() for item in items):
        raise MigrationError()


def validate_history(history, items: tuple[Revision, ...]):
    validate_revisions(items)
    expected = [(item.version, item.name, item.checksum) for item in items[:len(history)]]
    if list(history) != expected:
        raise MigrationError()


async def read_history(connection, schema: str):
    cursor = await connection.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
        "ON c.relnamespace=n.oid WHERE n.nspname=%s AND c.relname='schema_migrations')", (schema,))
    if not (await cursor.fetchone())[0]:
        cursor = await connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=%s)", (schema,))
        if (await cursor.fetchone())[0]:
            raise MigrationError()  # Do not adopt an untracked populated schema.
        return []
    cursor = await connection.execute(sql.SQL(
        "SELECT version, name, checksum FROM {}.schema_migrations ORDER BY version"
    ).format(sql.Identifier(schema)))
    history = await cursor.fetchall()
    # Atomic initialization cannot leave an empty ledger after success. An
    # existing empty ledger is not an ownership marker and must not be adopted.
    if not history:
        raise MigrationError()
    return history


async def migrate(target: ConnectionTarget, *, upgrade: bool = False,
                  schema: str = DEFAULT_SCHEMA, target_version: int | None = None,
                  items: tuple[Revision, ...] | None = None) -> tuple[int, int]:
    """All pending SQL and its ledger commit together; no automatic downgrade."""
    items = revisions() if items is None else items
    validate_revisions(items)
    head = len(items) if target_version is None else target_version
    if type(head) is not int or not 0 <= head <= len(items):
        raise MigrationError()
    # Keep tests/internal callers from selecting system or foreign schemas.
    if schema != DEFAULT_SCHEMA and not re.fullmatch(r"db02_probe_[a-f0-9]{32}", schema):
        raise MigrationError()
    lock_key = int.from_bytes(sha256(("enterprise-ai:" + schema).encode()).digest()[:8], "big", signed=True)
    try:
        async with direct_connection(target, timeout=60) as connection:
            async with bounded_transaction(connection):
                await connection.execute("SET LOCAL search_path = pg_catalog")
                await connection.execute("SET LOCAL statement_timeout = '20s'")
                await connection.execute("SET LOCAL lock_timeout = '5s'")
                await connection.execute("SET LOCAL idle_in_transaction_session_timeout = '20s'")
                if upgrade:
                    await connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
                else:
                    await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                history = await read_history(connection, schema)
                validate_history(history, items)
                current = len(history)
                if head < current:
                    raise MigrationError()
                if not upgrade:
                    return current, head
                if current == head:
                    return current, head
                ident = sql.Identifier(schema)
                if current == 0:
                    # No IF NOT EXISTS: a foreign creator winning the namespace
                    # race must fail this transaction instead of being adopted.
                    await connection.execute(sql.SQL("CREATE SCHEMA {}").format(ident))
                    await connection.execute(sql.SQL("CREATE TABLE {}.schema_migrations ("
                        "version integer PRIMARY KEY CHECK(version > 0), name text NOT NULL UNIQUE, "
                        "checksum text NOT NULL CHECK(checksum ~ '^[a-f0-9]{{64}}$'), "
                        "applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP)").format(ident))
                for item in items[current:head]:
                    await connection.execute(sql.SQL(item.source).format(schema=ident), prepare=False)
                    await connection.execute(sql.SQL(
                        "INSERT INTO {}.schema_migrations (version,name,checksum) VALUES (%s,%s,%s)"
                    ).format(ident), (item.version, item.name, item.checksum))
                return head, head
    except (psycopg.Error, TimeoutError, DatabaseUnavailable):
        raise MigrationError() from None
