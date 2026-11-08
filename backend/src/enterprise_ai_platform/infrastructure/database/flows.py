from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.flows.ports import FlowFailure


class PostgresFlowRepository:
    def __init__(self,database):self.database=database
    async def call(self,operation,payload):
        try:
            async with self.database.transaction() as connection:
                value=(await (await connection.execute(sql.SQL('SELECT {}.flow_call(%s,%s)').format(sql.Identifier(self.database.schema)),(operation,Jsonb(payload)))).fetchone())[0]
            if value.get('status')!='ok':raise FlowFailure(value.get('status'))
            return {k:v for k,v in value.items() if k!='status'}
        except FlowFailure:raise
        except Exception:raise FlowFailure() from None


class PostgresLockedClaimBindings:
    def __init__(self,database):self.database=database
    async def verify(self,actor,payload,policy,trip_date,claim_ref):
        try:
            async with self.database.transaction() as connection:
                value=(await (await connection.execute(sql.SQL('SELECT {}.flow_locked_call(%s,%s,%s,%s,%s)').format(sql.Identifier(self.database.schema)),(Jsonb(actor),Jsonb(payload),Jsonb(policy),trip_date,claim_ref))).fetchone())[0]
            return value is True
        except Exception:return False
