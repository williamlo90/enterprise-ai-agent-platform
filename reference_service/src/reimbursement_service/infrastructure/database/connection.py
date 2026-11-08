"""Bounded service-owned pool; every checkout starts without ambient authority."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import math
import sys
import time
import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool
from reimbursement_service.infrastructure.database.target import Target, TargetError, safe_driver_logging, schema_and_role
from reimbursement_service.modules.system.domain import ActorContext, DatabaseUnavailable


@dataclass(frozen=True)
class Limits:
    checkout_seconds: float = 5
    transaction_seconds: float = 10
    cleanup_seconds: float = 5
    statement_ms: int = 4000
    lock_ms: int = 1000

    def __post_init__(self):
        for value in (self.checkout_seconds, self.transaction_seconds, self.cleanup_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 60:
                raise ValueError("Invalid service database limit.")
        for value in (self.statement_ms, self.lock_ms):
            if type(value) is not int or not 0 < value <= 60000:
                raise ValueError("Invalid service database limit.")


async def finish_cleanup(awaitable):
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


@asynccontextmanager
async def bounded_transaction(connection, cleanup_seconds=5):
    transaction = connection.transaction()
    await transaction.__aenter__()
    try:
        yield
    except BaseException:
        async with asyncio.timeout(cleanup_seconds):
            await transaction.__aexit__(*sys.exc_info())
        raise
    else:
        async with asyncio.timeout(cleanup_seconds):
            await transaction.__aexit__(None, None, None)


@asynccontextmanager
async def direct_connection(target: Target, timeout=30):
    safe_driver_logging()
    connection = None
    try:
        async with asyncio.timeout(timeout):
            connection = await psycopg.AsyncConnection.connect(**target.kwargs())
            if not connection.pgconn.ssl_in_use:
                raise DatabaseUnavailable()
            yield connection
    except (psycopg.Error, TimeoutError):
        raise DatabaseUnavailable() from None
    finally:
        if connection is not None:
            async def cleanup():
                async with asyncio.timeout(5):
                    await connection.close()
            await finish_cleanup(cleanup())


class RuntimeDatabase:
    def __init__(self, target: Target, schema="reimbursement", *, max_size=2,
                 limits=None, pool_factory=AsyncConnectionPool):
        if target.user != schema_and_role(schema) or type(max_size) is not int or not 1 <= max_size <= 2:
            raise TargetError()
        safe_driver_logging()
        self.schema = schema
        self.limits = limits or Limits()
        self.pool = pool_factory(conninfo="", kwargs=target.kwargs(), open=False, min_size=0,
                                 max_size=max_size, num_workers=1, max_waiting=8,
                                 timeout=self.limits.checkout_seconds, reconnect_timeout=5,
                                 check=AsyncConnectionPool.check_connection,
                                 name="reimbursement-runtime")

    async def open(self):
        try:
            await self.pool.open()
            async with self.transaction():
                pass  # min_size=0 needs an actual checkout, TLS and schema/role proof.
        except BaseException:
            await self.close()
            raise

    async def close(self):
        async def cleanup():
            async with asyncio.timeout(self.limits.cleanup_seconds):
                await self.pool.close(timeout=self.limits.cleanup_seconds)
        await finish_cleanup(cleanup())
        if getattr(self,'security_journal',None) is not None:self.security_journal.close()

    async def guard(self, connection):
        if not connection.pgconn.ssl_in_use:
            raise DatabaseUnavailable()
        row = await (await connection.execute(sql.SQL(
            "SELECT current_user=session_user, {}.runtime_ready()"
        ).format(sql.Identifier(self.schema)))).fetchone()
        if row != (True, True):
            raise DatabaseUnavailable()

    async def ping(self):
        try:
            async with self.transaction():
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
            async with asyncio.timeout(self.limits.cleanup_seconds):
                await connection.close()
            await self.close()
            raise DatabaseUnavailable() from None

    @asynccontextmanager
    async def transaction(self, context: ActorContext | None = None, *, deadline=None):
        if context is not None and type(context) is not ActorContext:
            raise ValueError("Internal actor context required.")
        if deadline is not None and (type(deadline) not in (int,float) or not math.isfinite(deadline) or time.time()>=deadline):
            raise DatabaseUnavailable()
        connection = None
        discard = False
        journal_events = []
        try:
            async with asyncio.timeout(self.limits.transaction_seconds):
                connection = await self.pool.getconn(timeout=self.limits.checkout_seconds)
                async with bounded_transaction(connection, self.limits.cleanup_seconds):
                    await connection.execute(
                        "SELECT set_config('search_path','pg_catalog',true), "
                        "set_config('row_security','on',true), "
                        "set_config('statement_timeout',%s,true), set_config('lock_timeout',%s,true), "
                        "set_config('idle_in_transaction_session_timeout','10000',true), "
                        "set_config('reimbursement.catalog_action','',true), "
                        "set_config('reimbursement.catalog_digest','',true), "
                        "set_config('reimbursement.tenant_id',%s,true), "
                        "set_config('reimbursement.actor_id',%s,true)",
                        (str(self.limits.statement_ms), str(self.limits.lock_ms),
                         "" if context is None else str(context.tenant_id),
                         "" if context is None else str(context.actor_id)),
                    )
                    await self.guard(connection)
                    yield connection
                    if getattr(self,'security_journal',None) is not None:
                        journal_events=await self.security_journal.before_commit(connection,self.schema)
                    await self.guard(connection)
                    if deadline is not None and time.time()>=deadline:
                        raise DatabaseUnavailable()
                if journal_events:await self.security_journal.after_commit(journal_events)
        except asyncio.CancelledError:
            discard = True
            raise
        except (psycopg.Error, TimeoutError):
            discard = True
            raise DatabaseUnavailable() from None
        finally:
            if connection is not None:
                await finish_cleanup(self._release(connection, discard))
