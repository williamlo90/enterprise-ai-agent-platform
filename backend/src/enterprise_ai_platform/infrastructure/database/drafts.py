from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.drafts.ports import DraftFailure
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure
from enterprise_ai_platform.infrastructure.database.browser_context import before_read,after_read,committed_read


class PostgresDraftRepository:
    def __init__(self,database):self.database=database
    async def call(self,token,operation,payload):
        try:
            async with self.database.transaction() as connection:
                await before_read(self.database,connection,token,'drafts')
                result=(await (await connection.execute(sql.SQL('SELECT {}.draft_call(%s,%s,%s)').format(sql.Identifier(self.database.schema)),(token,operation,Jsonb(payload)))).fetchone())[0]
                bound=await after_read(self.database,connection,token,'drafts') if result.get('status')=='ok' else None
            if result.get('status')!='ok':raise DraftFailure({'invalid':400,'unauthenticated':401,'forbidden':403,'missing':404,'conflict':409,'incomplete':422}.get(result.get('status'),503))
            committed_read(token,'drafts',operation,bound)
            return {k:v for k,v in result.items() if k!='status'}
        except (DraftFailure,BrowserContextFailure):raise
        except Exception:raise DraftFailure(503) from None
