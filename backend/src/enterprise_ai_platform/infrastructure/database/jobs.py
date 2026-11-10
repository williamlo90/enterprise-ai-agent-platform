"""Restricted RPC adapter. Enqueue may share a feature owner's transaction."""
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.jobs.ports import JobFailure
from enterprise_ai_platform.infrastructure.database.connection import rpc_result


class PostgresJobRepository:
    def __init__(self, database, *, connection=None):
        self.database, self.connection = database, connection

    async def _execute(self, connection, operation, payload):
        return await rpc_result(connection,sql.SQL('SELECT {}.jobs_call(%s,%s)').format(sql.Identifier(self.database.schema)), (operation, Jsonb(payload)))

    async def call(self, operation, payload):
        try:
            if self.connection is not None:
                # Failures must abort the surrounding feature UoW, never commit partial work.
                result = await self._execute(self.connection, operation, payload)
                if result.get('status') != 'ok':
                    raise JobFailure(result.get('status', 'unavailable'))
                return result
            async with self.database.transaction() as connection:
                return await self._execute(connection, operation, payload)
        except JobFailure:
            raise
        except Exception:
            raise JobFailure() from None
