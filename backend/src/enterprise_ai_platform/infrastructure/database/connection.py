"""Bounded PostgreSQL transactions with backend-owned tenant/actor context."""

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
import sys
from urllib.parse import parse_qsl, unquote, urlsplit

import certifi
import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from enterprise_ai_platform.modules.identity.domain import TenantActorContext


class DatabaseUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Database operation unavailable.")


class DatabaseConfigurationError(ValueError):
    def __init__(self):
        super().__init__("Missing or invalid database configuration.")


class SafeDatabaseLog(logging.Filter):
    """libpq diagnostics may contain credentials, hostnames and SQL input."""

    def filter(self, record):
        record.msg = "Database driver event."
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def install_safe_database_logging():
    # psycopg currently emits pool events on psycopg.pool. Cover its other
    # registered child loggers too, since ancestor filters do not filter children.
    for name in {"psycopg", "psycopg.pool", "psycopg.transaction", "psycopg.waiting",
                 "psycopg_pool.sched", "psycopg_pool.sched_async"}:
        logger = logging.getLogger(name)
        if not any(isinstance(item, SafeDatabaseLog) for item in logger.filters):
            logger.addFilter(SafeDatabaseLog())


@dataclass(frozen=True, repr=False)
class ConnectionTarget:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    channel_binding: str = "prefer"

    def __repr__(self):
        return "ConnectionTarget(<redacted>)"

    def kwargs(self):
        # Never pass the original URI: no service/options/hostaddr/ssl override.
        return dict(host=self.host, port=self.port, dbname=self.dbname,
                    user=self.user, password=self.password, sslmode="verify-full",
                    sslrootcert=certifi.where(), channel_binding=self.channel_binding,
                    gssencmode="disable", connect_timeout=5, autocommit=True, prepare_threshold=None)


def parse_target(value: str, *, direct: bool = False) -> ConnectionTarget:
    try:
        if not value or re.search(r"[\x00-\x20\x7f]", value) or re.search(r"%(?![0-9a-fA-F]{2})", value):
            raise ValueError
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme not in {"postgres", "postgresql"} or not host
                or parts.fragment or not parts.username or not parts.password
                or not re.fullmatch(r"[A-Za-z0-9.-]+", host)
                or not parts.path.startswith("/") or len(parts.path) < 2):
            raise ValueError
        values = [unquote(item, errors="strict") for item in
                  (parts.username, parts.password, parts.path[1:])]
        if any(not item or re.search(r"[\x00-\x1f\x7f]", item) for item in values):
            raise ValueError
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        options = dict(pairs)
        if len(options) != len(pairs) or set(options) - {"sslmode", "channel_binding"}:
            raise ValueError
        if options.get("sslmode", "verify-full") not in {"require", "verify-full"}:
            raise ValueError
        binding = options.get("channel_binding", "prefer")
        if binding not in {"prefer", "require"}:
            raise ValueError
        if direct and "-pooler" in host:
            raise ValueError
        port = 5432 if parts.port is None else parts.port
        if not 1 <= port <= 65535:
            raise ValueError
        return ConnectionTarget(host.lower(), port, values[2], values[0], values[1], binding)
    except (ValueError, UnicodeError):
        raise DatabaseConfigurationError() from None


def verify_same_database(runtime: ConnectionTarget, direct: ConnectionTarget):
    # Neon pooled and direct endpoints differ by -pooler; other providers may
    # use the same hostname with different credentials/ports only if matching.
    if (runtime.host.replace("-pooler.", "."), runtime.port, runtime.dbname) != (
            direct.host, direct.port, direct.dbname):
        raise DatabaseConfigurationError()


@dataclass(frozen=True)
class DatabaseLimits:
    checkout_seconds: float = 5
    transaction_seconds: float = 15
    cleanup_seconds: float = 5
    statement_ms: int = 5000
    lock_ms: int = 2000
    idle_ms: int = 10000

    def __post_init__(self):
        for value in (self.checkout_seconds, self.transaction_seconds, self.cleanup_seconds,
                      self.statement_ms, self.lock_ms, self.idle_ms):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 60000:
                raise ValueError("Invalid database limit.")


async def finish_cleanup(awaitable):
    """Wait for bounded cleanup despite repeated cancellation, then propagate it."""
    task = asyncio.create_task(awaitable)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
    result = task.result()
    if cancelled is not None:
        raise cancelled
    return result


class RuntimeDatabase:
    def __init__(self, target: ConnectionTarget, *, limits: DatabaseLimits | None = None,
                 pool_factory=AsyncConnectionPool, schema: str = "platform", max_size: int = 2):
        if schema != "platform" and not re.fullmatch(r"db03_probe_[a-f0-9]{32}", schema):
            raise DatabaseConfigurationError()
        if type(max_size) is not int or not 1 <= max_size <= 2:
            raise DatabaseConfigurationError()
        install_safe_database_logging()
        self.schema = schema
        self.limits = limits or DatabaseLimits()
        self.pool = pool_factory(
            conninfo="", kwargs=target.kwargs(), open=False, min_size=0, max_size=max_size,
            max_waiting=8, timeout=self.limits.checkout_seconds, reconnect_timeout=5,
            check=AsyncConnectionPool.check_connection, name="platform-runtime",
        )

    async def open(self):
        try:
            async with asyncio.timeout(self.limits.checkout_seconds):
                await self.pool.open()
                # min_size=0: pool.wait() alone is not proof of connectivity.
                async with self.transaction() as connection:
                    await verify_tls(connection)
                    cursor = await connection.execute(sql.SQL(
                        "SELECT current_user=session_user, {}.runtime_role_is_valid()"
                    ).format(sql.Identifier(self.schema)))
                    if await cursor.fetchone() != (True, True):
                        raise DatabaseUnavailable()
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise DatabaseUnavailable() from None

    async def close(self):
        async def close_pool():
            async with asyncio.timeout(self.limits.cleanup_seconds):
                await self.pool.close(timeout=self.limits.cleanup_seconds)
        try:
            await finish_cleanup(close_pool())
        except (psycopg.Error, TimeoutError):
            raise DatabaseUnavailable() from None

    async def ping(self) -> bool:
        try:
            async with self.transaction() as connection:
                await connection.execute("SELECT 1")
            return True
        except DatabaseUnavailable:
            return False

    async def _release(self, connection, discard):
        try:
            async with asyncio.timeout(self.limits.cleanup_seconds):
                if discard:
                    await connection.close()
                await self.pool.putconn(connection)
        except (psycopg.Error, TimeoutError):
            # Fail the resource closed rather than silently lose pool capacity.
            try:
                async with asyncio.timeout(self.limits.cleanup_seconds):
                    await connection.close()
            except (psycopg.Error, TimeoutError):
                pass
            finally:
                await self.close()
            raise DatabaseUnavailable() from None

    @asynccontextmanager
    async def transaction(self, context: TenantActorContext | None = None):
        if context is not None and not isinstance(context, TenantActorContext):
            raise ValueError("A backend tenant/actor context is required.")
        connection = None
        discard = False
        try:
            async with asyncio.timeout(self.limits.transaction_seconds):
                async with asyncio.timeout(self.limits.checkout_seconds):
                    connection = await self.pool.getconn(timeout=self.limits.checkout_seconds)
                async with self._bounded_transaction(connection):
                    await connection.execute(
                        "SELECT set_config('statement_timeout', %s, true), "
                        "set_config('lock_timeout', %s, true), "
                        "set_config('idle_in_transaction_session_timeout', %s, true), "
                        "set_config('app.tenant_id', %s, true), "
                        "set_config('app.actor_id', %s, true)",
                        (str(self.limits.statement_ms), str(self.limits.lock_ms), str(self.limits.idle_ms),
                         "" if context is None else str(context.tenant_id),
                         "" if context is None else str(context.actor_id)),
                    )
                    yield connection
        except asyncio.CancelledError:
            discard = True
            raise
        except (psycopg.Error, TimeoutError):
            discard = True
            raise DatabaseUnavailable() from None
        finally:
            if connection is not None:
                # Cleanup has its own bound and survives caller cancellation.
                await finish_cleanup(self._release(connection, discard))

    @asynccontextmanager
    async def _bounded_transaction(self, connection):
        async with bounded_transaction(connection, cleanup_seconds=self.limits.cleanup_seconds):
            yield


@asynccontextmanager
async def bounded_transaction(connection, *, cleanup_seconds=5):
    """Transaction exit is bounded separately from its caller's work deadline."""
    transaction = connection.transaction()
    await transaction.__aenter__()
    try:
        yield
    except BaseException:
        # The work deadline can already have fired. Rollback still needs
        # a separate deadline, otherwise a stalled rollback can hang exit.
        async with asyncio.timeout(cleanup_seconds):
            await transaction.__aexit__(*sys.exc_info())
        raise
    else:
        async with asyncio.timeout(cleanup_seconds):
            await transaction.__aexit__(None, None, None)


async def verify_tls(connection):
    # Inspect the actual client TLS transport. pg_stat_ssl behind a pooler
    # describes pooler-to-server transport, not this application's connection.
    if not connection.pgconn.ssl_in_use:
        raise DatabaseUnavailable()
    cursor = await connection.execute("SELECT 1")
    row = await cursor.fetchone()
    if row != (1,):
        raise DatabaseUnavailable()


@asynccontextmanager
async def direct_connection(target: ConnectionTarget, *, timeout: float = 15):
    """Direct one-shot migration connection; never included in runtime resources."""
    install_safe_database_logging()
    connection = None
    try:
        async with asyncio.timeout(timeout):
            connection = await psycopg.AsyncConnection.connect(**target.kwargs())
            await verify_tls(connection)
            yield connection
    except (psycopg.Error, TimeoutError):
        raise DatabaseUnavailable() from None
    finally:
        if connection is not None:
            async def close_connection():
                async with asyncio.timeout(5):
                    await connection.close()
            try:
                await finish_cleanup(close_connection())
            except (psycopg.Error, TimeoutError):
                raise DatabaseUnavailable() from None
