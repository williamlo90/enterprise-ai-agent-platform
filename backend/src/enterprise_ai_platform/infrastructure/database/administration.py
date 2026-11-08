"""One atomic admin capability and final original G13 commit attestation."""
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.authorization.ports import AccessFailure
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure
from enterprise_ai_platform.infrastructure.database.browser_context import before_read,after_read,committed_read

class PostgresAdministrationRepository:
    def __init__(self,database):self.database=database
    async def call(self,token,operation,payload):
        try:
            async with self.database.transaction() as connection:
                if operation in {'member_create','member_active','role','config_write','provision'}:
                    # Establish AUTH03's tenant-writer/principal order before
                    # G13 takes original-session locks. This phase cannot write.
                    locked=(await (await connection.execute(sql.SQL('SELECT {}.admin_call(%s,%s,%s)').format(sql.Identifier(self.database.schema)),(token,'lock',Jsonb(dict(operation=operation,payload=payload))))).fetchone())[0]
                    if type(locked) is not dict or locked.get('status')!='ok':raise AccessFailure({'invalid':400,'unauthenticated':401,'forbidden':403,'missing':404,'conflict':409}.get(locked.get('status') if type(locked) is dict else None,503))
                await before_read(self.database,connection,token,'administration')
                result=(await (await connection.execute(sql.SQL('SELECT {}.admin_call(%s,%s,%s)').format(sql.Identifier(self.database.schema)),(token,operation,Jsonb(payload)))).fetchone())[0]
                if type(result) is not dict or result.get('status')!='ok':raise AccessFailure({'invalid':400,'unauthenticated':401,'forbidden':403,'missing':404,'not_found':404,'conflict':409}.get(result.get('status') if type(result) is dict else None,503))
                bound=await after_read(self.database,connection,token,'administration')
            committed_read(token,'administration',operation,bound)
            return {k:v for k,v in result.items() if k!='status'}
        except (AccessFailure,BrowserContextFailure):raise
        except Exception:raise AccessFailure(503) from None
