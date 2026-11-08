"""Read-only owner facts for drafts; does not issue human mutation authority."""
from fastapi import APIRouter,Request
from reimbursement_service.api.operations import strict_json
from reimbursement_service.modules.access.ports import AccessFailure,FailureKind,request_hash


def router(trips,claims,trip_dispatch,claim_dispatch):
    routes=APIRouter()
    @routes.post('/service/v1/draft-sources')
    async def source(request:Request):
        try:
            if request.scope.get('query_string'):raise ValueError
            body=strict_json(await request.body())
            if type(body) is not dict or set(body)!={'payload','trip_id','trip_payload'}:raise ValueError
            payload=body['payload'];action=payload['action'];obj=payload['object'];trip_id=body['trip_id']
            if action not in {'trip.read','claim.read'}:raise ValueError
            if action=='trip.read' and body['trip_payload'] is not None:raise ValueError
        except (KeyError,TypeError,ValueError,UnicodeError):raise AccessFailure(FailureKind.INVALID) from None
        dispatch=trip_dispatch if action=='trip.read' else claim_dispatch
        actor=await dispatch.authorize(request.scope['headers'],payload,action=action,object_kind='trip' if action=='trip.read' else 'claim',object_id=trip_id if action=='trip.read' else obj['id'],object_version=1)
        request.state.operation_authority_deadline=actor.expires_at
        locked=None;trip_actor=actor
        if action=='claim.read':
            view=await claims.read(payload,actor)
            if view['owner_id']!=actor.actor_id or view['trip_id']!=trip_id:raise AccessFailure(FailureKind.FORBIDDEN)
            if view['state']!='needs_information':raise AccessFailure(FailureKind.CONFLICT)
            locked={k:view[k] for k in ('id','owner_id','trip_id','state','version','revision','policy')}
            trip_tokens=[v for k,v in request.scope['headers'] if k.lower()==b'x-eai-trip-delegation']
            if len(trip_tokens)!=1:raise AccessFailure(FailureKind.INVALID)
            trip_headers=[(k,v) for k,v in request.scope['headers'] if k.lower() not in {b'x-eai-delegation',b'x-eai-trip-delegation'}]+[(b'x-eai-delegation',trip_tokens[0])]
            trip_actor=await trip_dispatch.authorize(trip_headers,body['trip_payload'],action='trip.read',object_kind='trip',object_id=trip_id,object_version=1)
            if any(getattr(trip_actor,k)!=getattr(actor,k) for k in ('tenant_id','actor_id','membership_id','access_revision')):raise AccessFailure(FailureKind.FORBIDDEN)
        trip,expiry=await trips.for_draft(trip_id,trip_actor)
        if locked is not None:
            fresh=await claims.read(payload,actor)
            if {k:fresh[k] for k in locked}!=locked:raise AccessFailure(FailureKind.CONFLICT)
        request.state.operation_authority_deadline=min(actor.expires_at,expiry)
        return dict(tenant_id=actor.tenant_id,actor_id=actor.actor_id,operation_id=actor.operation_id,request_hash=request_hash(payload),trip=trip,claim=locked,checked_at=actor.verified_at,expires_at=min(actor.expires_at,expiry))
    return routes
