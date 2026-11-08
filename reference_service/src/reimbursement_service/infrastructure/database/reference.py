"""Service reference capabilities. No platform table/credential sharing."""
import time
from uuid import UUID
from psycopg import sql
from psycopg.types.json import Jsonb
from reimbursement_service.modules.access.domain import AccessFailure,FailureKind,CatalogAuthority
from reimbursement_service.modules.policies.domain import Package,request_digest,encoded
from reimbursement_service.modules.trips.domain import Trip,identifier,summary,listing_digest
from reimbursement_service.modules.system.domain import ActorContext,DatabaseUnavailable


class ReferenceRepository:
    def __init__(self,database,*,clock=time.time):self.database,self.clock=database,clock

    async def call(self,function,operation,action,digest,payload,authority):
        if self.clock()>=authority.expires_at:raise AccessFailure(FailureKind.UNAVAILABLE)
        try:
            context=ActorContext(UUID(authority.tenant_id),UUID(authority.actor_id))
            async with self.database.transaction(context,deadline=authority.expires_at) as connection:
                await connection.execute("SELECT set_config('reimbursement.catalog_action',%s,true),set_config('reimbursement.catalog_digest',%s,true)",(action,digest))
                cursor=await connection.execute(sql.SQL('SELECT {}.{}(%s,%s)').format(
                    sql.Identifier(self.database.schema),sql.Identifier(function)),
                    (operation,Jsonb({**payload,'request_digest':digest,'expires_at':authority.expires_at})))
                value=(await cursor.fetchone())[0]
                if type(value) is not dict or 'status' not in value:raise AccessFailure(FailureKind.PROTOCOL)
                if value.get('status')!='ok':
                    if value.get('status')=='not_found':raise MissingReference()
                    raise AccessFailure({'invalid':FailureKind.INVALID,'unauthenticated':FailureKind.UNAUTHENTICATED,
                        'forbidden':FailureKind.FORBIDDEN,'conflict':FailureKind.CONFLICT}.get(value.get('status'),FailureKind.UNAVAILABLE))
                if set(value)!={'status','result'}:raise AccessFailure(FailureKind.PROTOCOL)
                result=value['result']
                if type(result) is not dict:raise AccessFailure(FailureKind.PROTOCOL)
                if self.clock()>=authority.expires_at:raise AccessFailure(FailureKind.UNAVAILABLE)
            return result
        except DatabaseUnavailable:raise AccessFailure(FailureKind.UNAVAILABLE) from None


class MissingReference(AccessFailure):
    def __init__(self):
        super().__init__(FailureKind.FORBIDDEN)
        self.status=404


class PostgresTrips(ReferenceRepository):
    async def detail(self,trip_id,actor):
        if actor.scope!='actor_only' or actor.action!='trip.read':raise AccessFailure(FailureKind.FORBIDDEN)
        try:identifier(trip_id)
        except ValueError:raise AccessFailure(FailureKind.INVALID) from None
        result=await self.call('trip_call','detail','trip.read',actor.request_hash,dict(trip_id=trip_id),actor)
        try:
            trip=Trip.from_wire(result)
            if trip.id!=trip_id:raise ValueError
            return trip.wire()
        except (TypeError,ValueError):raise AccessFailure(FailureKind.PROTOCOL) from None

    async def listing(self,after,limit,authority):
        if type(authority) is not CatalogAuthority or authority.binding.action!='trips.list':raise AccessFailure(FailureKind.FORBIDDEN)
        if (type(limit) is not int or not 1<=limit<=50 or authority.binding.request_digest!=listing_digest(after,limit)):
            raise AccessFailure(FailureKind.FORBIDDEN)
        result=await self.call('trip_call','list','trips.list',authority.binding.request_digest,dict(after=after,limit=limit),authority)
        try:
            if type(result) is not dict or set(result)!={'items','next_cursor'} or type(result['items']) is not list or len(result['items'])>limit:raise ValueError
            for row in result['items']:summary(row)
            ids=[row['id'] for row in result['items']]
            if ids!=sorted(set(ids)) or (after is not None and any(value<=after for value in ids)):raise ValueError
            if result['next_cursor'] is not None:
                identifier(result['next_cursor'])
                if not result['items'] or result['next_cursor']!=result['items'][-1]['id']:raise ValueError
            return result
        except (TypeError,ValueError):raise AccessFailure(FailureKind.PROTOCOL) from None


class PostgresPackages(ReferenceRepository):
    async def receive(self,package,key,authority):
        if type(package) is not Package or type(authority) is not CatalogAuthority:raise AccessFailure(FailureKind.FORBIDDEN)
        binding=authority.binding
        if (binding.action!='policy.publish' or binding.key!=key or binding.object_id!=package.package_id
                or binding.object_version!=package.package_version or authority.tenant_id!=package.tenant_id
                or binding.request_digest!=request_digest(package.package_id,package.package_version,package.canonical)):
            raise AccessFailure(FailureKind.FORBIDDEN)
        result=await self.call('package_call','receive','policy.publish',binding.request_digest,
            dict(key=key,operation_id=binding.operation_id,canonical=encoded(package.content).decode(),digest=package.digest),authority)
        try:
            if (type(result) is not dict or set(result)!={'package_id','package_version','digest','received_at','state'}
                    or result['package_id']!=package.package_id or type(result['package_version']) is not int
                    or result['package_version']!=package.package_version or result['digest']!=package.digest
                    or type(result['received_at']) is not int or result['received_at']<=0 or result['state']!='received'):raise ValueError
            return result
        except (ValueError,TypeError):raise AccessFailure(FailureKind.PROTOCOL) from None
