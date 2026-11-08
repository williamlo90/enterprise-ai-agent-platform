"""Private current receipt relationship, never a general claim writer."""
from fastapi import APIRouter,Request
from reimbursement_service.api.operations import strict_json
from reimbursement_service.modules.claims.ports import ClaimFailure
from reimbursement_service.modules.claims.service import ClaimsService
from reimbursement_service.modules.access.service import AccessService


def router(claims:ClaimsService,dispatch:AccessService,*,decision_sources=False):
    routes=APIRouter()

    @routes.post('/service/v1/claims/{claim_id}/access-check')
    async def access_check(claim_id:str,request:Request):
        try:
            if request.scope.get('query_string'):raise ValueError
            body=strict_json(await request.body())
            if type(body) is not dict or set(body)!={'payload'}:raise ValueError
            payload=body['payload'];obj=payload['object']
            version=obj['version']
        except (ValueError,KeyError,TypeError,UnicodeError):raise ClaimFailure(400) from None
        actor=await dispatch.authorize(request.scope['headers'],payload,action='claim.read',
            object_kind='claim',object_id=claim_id,object_version=version)
        request.scope['state']['operation_authority_deadline']=actor.expires_at
        result=await claims.access_check(payload,actor)
        request.scope['state']['operation_authority_deadline']=min(actor.expires_at,result['expires_at'])
        return result

    if decision_sources:
        @routes.post('/service/v1/claims/{claim_id}/decision-source')
        async def decision_source(claim_id:str,request:Request):
            try:
                if request.scope.get('query_string'):raise ValueError
                body=strict_json(await request.body())
                if type(body) is not dict or set(body)!={'payload'}:raise ValueError
                payload=body['payload']
            except (ValueError,KeyError,TypeError,UnicodeError):raise ClaimFailure(400) from None
            actor=await dispatch.authorize(request.scope['headers'],payload,action='claim.read',object_kind='claim',object_id=claim_id,object_version=1)
            request.state.operation_authority_deadline=actor.expires_at
            value=await claims.read(payload,actor)
            if value['owner_id']==actor.actor_id or value['reviewer_id']!=actor.actor_id:raise ClaimFailure(403)
            if value['state']!='pending_approval':raise ClaimFailure(409)
            claim={k:value[k] for k in ('id','owner_id','trip_id','trip_date','state','version','revision','active_stage','reviewer_id','policy')}
            from reimbursement_service.modules.access.ports import request_hash
            return dict(tenant_id=actor.tenant_id,actor_id=actor.actor_id,operation_id=actor.operation_id,request_hash=request_hash(payload),claim=claim,checked_at=actor.verified_at,expires_at=actor.expires_at)
    return routes
