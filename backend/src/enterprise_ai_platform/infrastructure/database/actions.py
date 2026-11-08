from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.actions.ports import ActionFailure
from enterprise_ai_platform.modules.delegation.ports import HumanReceipt
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure
from enterprise_ai_platform.infrastructure.database.browser_context import before_read,after_read,committed_read


class PostgresActionRepository:
    def __init__(self,database):self.database=database
    async def call(self,token,operation,payload):
        try:
            async with self.database.transaction() as connection:
                await before_read(self.database,connection,token,'actions')
                result=(await (await connection.execute(sql.SQL('SELECT {}.action_call(%s,%s,%s)').format(sql.Identifier(self.database.schema)),(token,operation,Jsonb(payload)))).fetchone())[0]
                bound=await after_read(self.database,connection,token,'actions') if result.get('status')=='ok' else None
            if result.get('status')!='ok':raise ActionFailure({'invalid':400,'unauthenticated':401,'forbidden':403,'missing':404,'conflict':409,'incomplete':422}.get(result.get('status'),503))
            committed_read(token,'actions',operation,bound)
            return {k:v for k,v in result.items() if k!='status'}
        except (ActionFailure,BrowserContextFailure):raise
        except Exception:raise ActionFailure(503) from None


class PostgresHumanReceiptReader:
    def __init__(self,database):self.database=database
    async def get(self,receipt_id):
        try:
            async with self.database.transaction() as connection:
                value=(await (await connection.execute(sql.SQL('SELECT {}.human_receipt_get(%s)').format(sql.Identifier(self.database.schema)),(receipt_id,))).fetchone())[0]
            return None if value is None else HumanReceipt(**value)
        except Exception:raise ActionFailure(503) from None
