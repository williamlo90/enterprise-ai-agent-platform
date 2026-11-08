from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.organization.ports import OrganizationFailure
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure
from enterprise_ai_platform.infrastructure.database.browser_context import before_read,after_read,committed_read

class PostgresOrganizationRepository:
    def __init__(self,database):self.database=database
    async def call(self,token,operation,payload):
        try:
            async with self.database.transaction() as connection:
                await before_read(self.database,connection,token,'organization')
                result=(await (await connection.execute(sql.SQL('SELECT {}.organization_call(%s,%s,%s)').format(sql.Identifier(self.database.schema)),(token,operation,Jsonb(payload)))).fetchone())[0]
                if not isinstance(result,dict) or result.get('status')!='ok':raise OrganizationFailure({'unauthenticated':401,'forbidden':403,'missing':404,'invalid':400,'conflict':409,'capacity':429}.get(result.get('status') if isinstance(result,dict) else None,503))
                bound=await after_read(self.database,connection,token,'organization')
            committed_read(token,'organization',operation,bound)
            return {key:value for key,value in result.items() if key!='status'}
        except BrowserContextFailure:raise
        except OrganizationFailure:raise
        except Exception:raise OrganizationFailure(503) from None
