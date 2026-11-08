"""Private catalog-bound view: routing/filter is part of the signed request."""
import hashlib
import json
from fastapi import APIRouter,Request
from reimbursement_service.api.operations import strict_json
from reimbursement_service.modules.access.ports import CatalogBinding
from reimbursement_service.modules.claims.ports import ClaimFailure
from reimbursement_service.modules.access.ports import identifier,number


def router(repository,scopes):
    routes=APIRouter()
    @routes.post('/service/v1/workspace')
    async def read(request:Request):
        try:
            if request.scope.get('query_string'):raise ValueError
            value=strict_json(await request.body())
            if type(value) is not dict or set(value)!={'view','claim_id','after','limit','operation_id'} or value['view'] not in {'mine','approvals'}:raise ValueError
            for k in ('claim_id','after'):
                if value[k] is not None:identifier(value[k])
            identifier(value['operation_id']);number(value['limit'],20)
            if value['claim_id'] is not None and (value['after'] is not None or value['limit']!=1):raise ValueError
            query={k:value[k] for k in ('view','claim_id','after','limit')}
            digest=hashlib.sha256(b'reimbursement:workspace:v1:'+json.dumps(query,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            binding=CatalogBinding('workflow.read',value['claim_id'],1,digest,value['operation_id'],None)
        except (ValueError,KeyError,TypeError):raise ClaimFailure(400) from None
        authority=await scopes.authorize(request.scope['headers'],binding)
        request.state.operation_authority_deadline=authority.expires_at
        result=await repository.read(value['view'],{k:v for k,v in query.items() if k!='view'},authority)
        return dict(tenant_id=authority.tenant_id,actor_id=authority.actor_id,operation_id=authority.binding.operation_id,request_hash=digest,checked_at=authority.verified_at,expires_at=authority.expires_at,result=result)
    return routes
