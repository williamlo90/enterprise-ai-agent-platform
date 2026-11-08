"""Atomic ledger/feature UoW. No outbound HTTP runs while locks are held."""
import time
from uuid import UUID
from psycopg import sql
from psycopg.types.json import Jsonb
from reimbursement_service.modules.operations.domain import OperationFailure,Result,Terminal
from reimbursement_service.modules.system.domain import ActorContext,DatabaseUnavailable


class PostgresOperations:
    def __init__(self,database,*,clock=time.time):self.database,self.clock=database,clock

    def current(self,actor):
        if actor.scope!='actor_only' or self.clock()>=actor.expires_at:raise OperationFailure(401)

    async def call(self,connection,operation,payload):
        value=(await (await connection.execute(sql.SQL('SELECT {}.operation_call(%s,%s)').format(
            sql.Identifier(self.database.schema)),(operation,Jsonb(payload)))).fetchone())[0]
        if value.get('status') not in {'ok','absent'}:
            raise OperationFailure({'invalid':400,'unauthenticated':401,'not_found':404,'conflict':409}.get(value.get('status'),503))
        return value

    async def _write(self,identity,actor,work=None):
        self.current(actor)
        if (identity.tenant_id,identity.actor_id)!=(actor.tenant_id,actor.actor_id):raise OperationFailure(404)
        context=ActorContext(UUID(actor.tenant_id),UUID(actor.actor_id))
        payload=dict(identity=identity.wire(),expires_at=actor.expires_at)
        try:
            async with self.database.transaction(context,deadline=actor.expires_at) as connection:
                existing=await self.call(connection,'lock',payload)
                self.current(actor)
                if existing['status']=='ok':terminal=Terminal.from_wire(existing['terminal'])
                else:
                    if work is None:state,result='sealed_no_commit',Result(None,None,'sealed_no_commit')
                    else:
                        state,result=await work.apply(connection)
                        if state not in {'committed','rejected'} or type(result) is not Result:raise OperationFailure(400)
                    self.current(actor)
                    stored=await self.call(connection,'finish',{**payload,'state':state,'result':result.wire(),'result_digest':result.digest})
                    terminal=Terminal.from_wire(stored['terminal'])
                self.current(actor)
            return terminal
        except DatabaseUnavailable:raise OperationFailure(503) from None
        except (ValueError,TypeError):raise OperationFailure(503) from None

    async def commit(self,identity,actor,work):return await self._write(identity,actor,work)

    async def existing(self,identity,actor):
        self.current(actor)
        if (identity.tenant_id,identity.actor_id)!=(actor.tenant_id,actor.actor_id):raise OperationFailure(404)
        try:
            async with self.database.transaction(ActorContext(UUID(actor.tenant_id),UUID(actor.actor_id)),deadline=actor.expires_at) as connection:
                try:value=await self.call(connection,'lookup',dict(operation_id=identity.operation_id,expires_at=actor.expires_at))
                except OperationFailure as error:
                    if error.status==404:return None
                    raise
                terminal=Terminal.from_wire(value['terminal'])
                self.current(actor)
                if terminal.identity!=identity:raise OperationFailure(409)
                return terminal
        except DatabaseUnavailable:raise OperationFailure(503) from None
        except (ValueError,TypeError):raise OperationFailure(503) from None

    async def seal(self,identity,actor):return await self._write(identity,actor)

    async def lookup(self,operation_id,actor):
        self.current(actor)
        try:
            async with self.database.transaction(ActorContext(UUID(actor.tenant_id),UUID(actor.actor_id)),deadline=actor.expires_at) as connection:
                value=await self.call(connection,'lookup',dict(operation_id=operation_id,expires_at=actor.expires_at))
                terminal=Terminal.from_wire(value['terminal'])
                self.current(actor)
            return terminal
        except DatabaseUnavailable:raise OperationFailure(503) from None
        except (ValueError,TypeError):raise OperationFailure(503) from None
