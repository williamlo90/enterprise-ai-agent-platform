from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.files.domain import FileFailure
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure
from enterprise_ai_platform.infrastructure.database.browser_context import before_read, after_read, committed_read


class PostgresFileRepository:
    def __init__(self,database):self.database=database

    async def claim_call(self,token,operation,payload):
        try:
            async with self.database.transaction() as connection:
                await before_read(self.database,connection,token,'files')
                result=(await (await connection.execute(sql.SQL('SELECT {}.receipt_file_call(%s,%s,%s)').format(sql.Identifier(self.database.schema)),
                    (token,operation,Jsonb(payload)))).fetchone())[0]
                bound=await after_read(self.database,connection,token,'files') if result.get('status')=='ok' else None
            if result.get('status')!='ok':raise FileFailure({'unauthenticated':401,'forbidden':403,'missing':404,'invalid':400,'conflict':409}.get(result.get('status'),503))
            committed_read(token,'files',operation,bound)
            return {k:v for k,v in result.items() if k!='status'}
        except (BrowserContextFailure,FileFailure):raise
        except Exception:raise FileFailure(503) from None

    async def call(self,token,operation,payload):
        try:
            async with self.database.transaction() as connection:
                await before_read(self.database,connection,token,'files')
                result=(await (await connection.execute(sql.SQL('SELECT {}.files_call(%s,%s,%s)').format(sql.Identifier(self.database.schema)),
                    (token,operation,Jsonb(payload)))).fetchone())[0]
                bound = await after_read(self.database,connection,token,'files') if result.get('status')=='ok' else None
            if result.get('status')!='ok':
                raise FileFailure({'unauthenticated':401,'forbidden':403,'missing':404,'invalid':400,'conflict':409,'capacity':429,'stale':409,'held':403}.get(result.get('status'),503))
            committed_read(token,'files',operation,bound)
            return {k:v for k,v in result.items() if k!='status'}
        except BrowserContextFailure:raise
        except FileFailure:raise
        except Exception:raise FileFailure(503) from None
