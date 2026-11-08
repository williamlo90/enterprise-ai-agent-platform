"""Narrow run-fenced worker bridge. No action confirmation capability."""
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.infrastructure.database.rag import PostgresRagRepository
from enterprise_ai_platform.modules.rag.ports import RagFailure

class PostgresAssistedRepository(PostgresRagRepository):
    async def execute(self,connection,operation,payload):
        row=(await (await connection.execute(sql.SQL('SELECT {}.assisted_call(%s,%s)').format(sql.Identifier(self.database.schema)),(operation,Jsonb({**payload,**self.binding})))) .fetchone())[0]
        if not isinstance(row,dict) or row.get('status')!='ok':raise RagFailure({'invalid':400,'forbidden':403,'stale':409,'conflict':409,'missing':404,'incomplete':422,'unauthenticated':401}.get(row.get('status') if isinstance(row,dict) else None,503))
        return row
