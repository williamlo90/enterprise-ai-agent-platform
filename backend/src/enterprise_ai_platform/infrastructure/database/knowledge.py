from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.knowledge.domain import KnowledgeFailure
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure
from enterprise_ai_platform.infrastructure.database.browser_context import before_read, after_read, committed_read

class PostgresKnowledgeRepository:
    def __init__(self,database):self.database=database
    async def call(self,token,operation,payload):
        try:
            async with self.database.transaction() as connection:
                await before_read(self.database,connection,token,'knowledge')
                result=(await (await connection.execute(sql.SQL('SELECT {}.knowledge_call(%s,%s,%s)').format(sql.Identifier(self.database.schema)),(token,operation,Jsonb(payload)))).fetchone())[0]
                bound = await after_read(self.database,connection,token,'knowledge') if result.get('status')=='ok' else None
            if result.get('status')!='ok':raise KnowledgeFailure({'unauthenticated':401,'forbidden':403,'missing':404,'invalid':400,'conflict':409,'capacity':429,'stale':409,'held':403}.get(result.get('status'),503))
            committed_read(token,'knowledge',operation,bound)
            return {k:v for k,v in result.items() if k!='status'}
        except BrowserContextFailure:raise
        except KnowledgeFailure:raise
        except Exception:raise KnowledgeFailure(503) from None
