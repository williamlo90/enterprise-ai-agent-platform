"""Current browser context and owned discovery, no HTTP inside owner UoW."""
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.actions.domain import ActionFailure
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure
from enterprise_ai_platform.infrastructure.database.browser_context import before_read,after_read,committed_read

class PostgresWorkspaceRepository:
    def __init__(self,database):self.database=database
    async def call(self,token,operation,payload,*,final=False):
        try:
            async with self.database.transaction() as connection:
                await before_read(self.database,connection,token,'workspace')
                result=(await (await connection.execute(sql.SQL('SELECT {}.workspace_call(%s,%s,%s)').format(sql.Identifier(self.database.schema)),(token,operation,Jsonb(payload)))).fetchone())[0]
                if not isinstance(result,dict) or result.get('status')!='ok':raise ActionFailure({'invalid':400,'unauthenticated':401,'forbidden':403,'conflict':409}.get(result.get('status') if isinstance(result,dict) else None,503))
                bound=await after_read(self.database,connection,token,'workspace')
            committed_read(token,'workspace',('workspace_read' if operation=='context' else operation) if final else 'context',bound)
            return {k:v for k,v in result.items() if k!='status'}
        except (ActionFailure,BrowserContextFailure):raise
        except Exception:raise ActionFailure(503) from None
