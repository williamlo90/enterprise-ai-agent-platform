import time
from reimbursement_service.modules.trips.ports import TripsRepository,Trip,identifier,listing_digest
from reimbursement_service.modules.access.ports import (
    ScopedAuthorityPort,CatalogBinding,CatalogAuthority,AccessFailure,FailureKind,canonical_payload,request_hash)


class TripsService:
    def __init__(self,repository:TripsRepository,scopes:ScopedAuthorityPort,*,clock=time.time):
        self.repository,self.scopes,self.clock=repository,scopes,clock

    def current(self,authority):
        if self.clock()>=authority.expires_at:raise AccessFailure(FailureKind.UNAVAILABLE)

    async def for_draft(self,trip_id,actor):
        try:identifier(trip_id)
        except ValueError:raise AccessFailure(FailureKind.INVALID) from None
        if actor.scope!='actor_only' or actor.action!='trip.read':raise AccessFailure(FailureKind.FORBIDDEN)
        self.current(actor)
        value=await self.repository.detail(trip_id,actor)
        self.current(actor)
        return value,actor.expires_at

    async def detail(self,trip_id,actor,payload):
        try:identifier(trip_id);canonical_payload(payload)
        except (ValueError,KeyError,TypeError):raise AccessFailure(FailureKind.INVALID) from None
        if (actor.scope!='actor_only' or actor.action!='trip.read' or payload['action']!='trip.read'
                or payload['object']!={'kind':'trip','id':trip_id,'version':1}
                or actor.request_hash!=request_hash(payload) or actor.actor_id!=payload['actor_id']
                or actor.tenant_id!=payload['tenant_id'] or actor.operation_id!=payload['operation_id']):raise AccessFailure(FailureKind.FORBIDDEN)
        self.current(actor)
        value=await self.repository.detail(trip_id,actor)
        self.current(actor)
        return value,actor.expires_at

    async def listing(self,headers,operation_id,after=None,limit=20):
        try:
            identifier(operation_id)
            if after is not None:identifier(after)
            if type(limit) is not int or not 1<=limit<=50:raise ValueError
            binding=CatalogBinding('trips.list',None,1,listing_digest(after,limit),operation_id,None)
        except (ValueError,TypeError):raise AccessFailure(FailureKind.INVALID) from None
        authority=await self.scopes.authorize(headers,binding)
        if type(authority) is not CatalogAuthority or authority.binding!=binding:raise AccessFailure(FailureKind.FORBIDDEN)
        if authority.verified_at>self.clock()+5:raise AccessFailure(FailureKind.FORBIDDEN)
        self.current(authority)
        value=await self.repository.listing(after,limit,authority)
        self.current(authority)
        return value,authority.expires_at
