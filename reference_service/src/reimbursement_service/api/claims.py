"""Private service writer; each exact mutation requires original human authority."""
from fastapi import APIRouter,Request
from reimbursement_service.api.operations import strict_json
from reimbursement_service.modules.claims.ports import ClaimFailure


def router(claims,dispatch):
    routes=APIRouter()
    @routes.post('/service/v1/claims/execute')
    async def execute(request:Request):
        try:
            if request.scope.get('query_string'):raise ValueError
            body=strict_json(await request.body())
            if type(body) is not dict or set(body)!={'payload','key'}:raise ValueError
            payload=body['payload'];obj=payload['object'];action=payload['action']
            if action not in {'claim.submit','claim.information','claim.decide'}:raise ValueError
        except (ValueError,KeyError,TypeError,UnicodeError):raise ClaimFailure(400) from None
        actor=await dispatch.authorize(request.scope['headers'],payload,action=action,object_kind='draft' if action=='claim.submit' else 'claim',object_id=obj['id'],object_version=obj['version'])
        request.state.operation_authority_deadline=actor.expires_at
        tokens=[v for k,v in request.scope['headers'] if k.lower()==b'x-eai-delegation']
        if len(tokens)!=1:raise ClaimFailure(401)
        value=await claims.execute(payload,body['key'],actor,context=dict(delegation=tokens[0].decode('ascii')))
        request.state.operation_authority_deadline=min(actor.expires_at,value['proof']['payload']['expires_at'])
        return value
    return routes
