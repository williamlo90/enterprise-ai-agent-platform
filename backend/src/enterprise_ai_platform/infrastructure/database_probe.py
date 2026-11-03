"""Explicit live connectivity/transaction proof using disposable synthetic data."""

import asyncio
from uuid import uuid4

from psycopg import sql

from enterprise_ai_platform.infrastructure.database.connection import (
    ConnectionTarget, DatabaseLimits, DatabaseUnavailable, RuntimeDatabase,
    direct_connection, verify_same_database,
)


async def run_probe(runtime: ConnectionTarget, direct: ConnectionTarget) -> dict[str, bool]:
    verify_same_database(runtime, direct)
    name = "db01_probe_" + uuid4().hex
    table = sql.Identifier("public", name)
    insert = sql.SQL("INSERT INTO {} (id) VALUES (%s)").format(table)
    select = sql.SQL("SELECT id FROM {} ORDER BY id").format(table)
    created = False
    database = RuntimeDatabase(runtime)
    short = RuntimeDatabase(runtime)
    try:
        async with direct_connection(direct) as connection:
            await connection.execute(sql.SQL("CREATE TABLE {} (id integer PRIMARY KEY)").format(table))
            created = True
            await connection.execute(sql.SQL("GRANT SELECT, INSERT ON {} TO {}").format(
                table, sql.Identifier(runtime.user)))
        await database.open()  # TLS verified on actual runtime endpoint.
        async with database.transaction() as connection:
            await connection.execute(insert, (1,))
        try:
            async with database.transaction() as connection:
                await connection.execute(insert, (2,))
                raise ValueError("Synthetic rollback.")
        except ValueError:
            pass
        try:
            async with database.transaction() as connection:
                await connection.execute("SELECT 1 / 0")
        except DatabaseUnavailable:
            pass
        else:
            raise DatabaseUnavailable()

        entered = asyncio.Event()

        async def cancelled_work():
            async with database.transaction() as connection:
                await connection.execute(insert, (3,))
                entered.set()
                await asyncio.sleep(30)

        task = asyncio.create_task(cancelled_work())
        try:
            async with asyncio.timeout(15):
                await entered.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Warm the connection before the short transaction deadline assertion.
        await short.open()
        short.limits = DatabaseLimits(transaction_seconds=2)
        try:
            async with short.transaction() as connection:
                await connection.execute(insert, (4,))
                await asyncio.sleep(3)
        except DatabaseUnavailable:
            pass
        else:
            raise DatabaseUnavailable()
        async with database.transaction() as connection:
            await connection.execute(insert, (5,))
            rows = await (await connection.execute(select)).fetchall()
            if rows != [(1,), (5,)]:
                raise DatabaseUnavailable()
        async with direct_connection(direct) as connection:
            if await (await connection.execute(select)).fetchall() != [(1,), (5,)]:
                raise DatabaseUnavailable()
        if not await short.ping():
            raise DatabaseUnavailable()
        return dict(tls=True, commit=True, rollback=True, error_reuse=True,
                    cancellation_cleanup=True, deadline_cleanup=True)
    finally:
        try:
            await short.close()
        finally:
            try:
                await database.close()
            finally:
                if created:
                    async with direct_connection(direct) as connection:
                        await connection.execute(sql.SQL("DROP TABLE {} ").format(table))
