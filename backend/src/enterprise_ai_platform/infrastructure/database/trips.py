from psycopg import sql
from enterprise_ai_platform.modules.trips.ports import TripFailure
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure
from enterprise_ai_platform.infrastructure.database.browser_context import before_read,after_read,committed_read


class PostgresTripRepository:
    def __init__(self,database):self.database=database
    async def context(self,token,operation):
        try:
            async with self.database.transaction() as connection:
                await before_read(self.database,connection,token,'trips')
                result=(await (await connection.execute(sql.SQL('SELECT {}.trip_catalog_call(%s)').format(sql.Identifier(self.database.schema)),(token,))).fetchone())[0]
                if type(result) is not dict or result.get('status')!='ok':raise TripFailure({'unauthenticated':401,'forbidden':403,'invalid':400}.get(result.get('status') if type(result) is dict else None,503))
                bound=await after_read(self.database,connection,token,'trips')
            committed_read(token,'trips',operation,bound)
            return {k:v for k,v in result.items() if k!='status'}
        except (TripFailure,BrowserContextFailure):raise
        except Exception:raise TripFailure(503) from None
