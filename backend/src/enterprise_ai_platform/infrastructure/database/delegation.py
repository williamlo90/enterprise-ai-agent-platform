from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.delegation.ports import DelegationFailure


class PostgresDelegationRepository:
    def __init__(self,database):self.database=database
    async def call(self,operation,payload):
        try:
            async with self.database.transaction() as connection:
                cursor=await connection.execute(sql.SQL("SELECT {}.delegation_call(%s,%s)").format(sql.Identifier(self.database.schema)),(operation,Jsonb(payload)))
                result=(await cursor.fetchone())[0]
            return result
        except Exception:raise DelegationFailure(503) from None
