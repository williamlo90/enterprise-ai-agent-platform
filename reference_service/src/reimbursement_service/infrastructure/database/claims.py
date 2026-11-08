"""Claim capability adapter. apply() uses the ledger-owned connection as-is."""
import time
from uuid import UUID
from psycopg import sql
from psycopg.types.json import Jsonb
from reimbursement_service.modules.claims.ports import ClaimFailure
from reimbursement_service.modules.system.domain import ActorContext,DatabaseUnavailable


class PostgresClaims:
    def __init__(self,database,*,clock=time.time):self.database,self.clock=database,clock

    async def apply(self,uow,operation,packet):
        # No transaction, pool checkout, provider call or external dependency here.
        return (await (await uow.execute(sql.SQL('SELECT {}.claim_call(%s,%s)').format(
            sql.Identifier(self.database.schema)),(operation,Jsonb(packet)))).fetchone())[0]

    async def _read(self,operation,packet,actor):
        if actor.scope!='actor_only' or self.clock()>=actor.expires_at:raise ClaimFailure(401)
        try:
            async with self.database.transaction(ActorContext(UUID(actor.tenant_id),UUID(actor.actor_id)),deadline=actor.expires_at) as uow:
                value=await self.apply(uow,operation,{**packet,'expires_at':actor.expires_at})
                if self.clock()>=actor.expires_at:raise ClaimFailure(401)
                if value.get('status')!='ok':raise ClaimFailure({'invalid':400,'unauthenticated':401,'forbidden':403,'missing':404}.get(value.get('status'),503))
                return {k:v for k,v in value.items() if k!='status'}
        except DatabaseUnavailable:raise ClaimFailure(503) from None
        except (ValueError,TypeError):raise ClaimFailure(503) from None

    async def facts(self,trip_id,policy,actor,*,item_ids=()):return await self._read('facts',dict(trip_id=trip_id,policy=policy,item_ids=list(item_ids)),actor)
    async def read(self,claim_id,actor):return (await self._read('read',dict(claim_id=claim_id),actor))['claim']
    async def relation(self,claim_id,actor):return (await self._read('relation',dict(claim_id=claim_id),actor))['claim']
