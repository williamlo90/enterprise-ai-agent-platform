"""Private receipt capabilities; each call owns a short bounded transaction."""
import time
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.receipts.ports import ReceiptFailure


class PostgresReceiptRepository:
    def __init__(self,database,*,clock=time.time):self.database,self.clock=database,clock

    async def _call(self,name,arguments):
        try:
            async with self.database.transaction() as connection:
                marks=sql.SQL(',').join(sql.Placeholder() for _ in arguments)
                result=(await (await connection.execute(sql.SQL('SELECT {}.{}({})').format(sql.Identifier(self.database.schema),sql.Identifier(name),marks),arguments)).fetchone())[0]
                if result.get('status')!='ok':raise ReceiptFailure({'invalid':400,'unauthenticated':401,'forbidden':403,'missing':404,'conflict':409}.get(result.get('status'),503))
                deadline=result.get('expires_at')
                if type(deadline) is not int or self.clock()>=deadline:raise ReceiptFailure(503)
            if self.clock()>=deadline:raise ReceiptFailure(503)
            return {k:v for k,v in result.items() if k!='status'}
        except ReceiptFailure:raise
        except Exception:raise ReceiptFailure(503) from None

    async def prepare(self,batch,payload,verified_reference,actor):
        auth=dict(actor=actor,credential_hash=verified_reference['credential_hash'],verification_ref=verified_reference)
        return await self._call('receipt_pin_prepare',(Jsonb(auth),Jsonb(dict(batch=batch.wire(),payload=payload))))

    async def lookup(self,credential_hash,tenant_id,operation_id):
        return await self._call('receipt_pin_lookup',(credential_hash,tenant_id,operation_id))

    async def finalise(self,credential_hash,settlement):
        return await self._call('receipt_pin_finalise',(credential_hash,Jsonb(settlement)))
