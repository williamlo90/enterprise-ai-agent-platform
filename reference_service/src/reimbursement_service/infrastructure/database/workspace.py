"""Current service workspace reads in one actor-scoped source transaction."""
from uuid import UUID
from psycopg import sql
from psycopg.types.json import Jsonb
from reimbursement_service.modules.system.domain import ActorContext
from reimbursement_service.modules.claims.ports import ClaimFailure

class WorkspaceRepository:
    def __init__(self,database):self.database=database
    async def read(self,view,query,authority):
        async with self.database.transaction(ActorContext(UUID(authority.tenant_id),UUID(authority.actor_id)),deadline=authority.expires_at) as connection:
            row=(await (await connection.execute(sql.SQL('SELECT {}.workspace_call(%s,%s)').format(sql.Identifier(self.database.schema)),(view,Jsonb({**query,'expires_at':authority.expires_at})))) .fetchone())[0]
            if not isinstance(row,dict) or row.get('status')!='ok':raise ClaimFailure({'missing':404,'invalid':400,'conflict':409,'unauthenticated':401}.get(row.get('status') if isinstance(row,dict) else None,503))
            return {k:v for k,v in row.items() if k!='status'}
