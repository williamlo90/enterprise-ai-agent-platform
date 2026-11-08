import json
from fastapi import APIRouter,Request
from enterprise_ai_platform.api.identity import cookie,browser_headers,SESSION_COOKIE
from enterprise_ai_platform.modules.actions.ports import ActionFailure

router=APIRouter()


def service(request):
    request.state.authorization_required=True
    if request.scope.get('query_string'):raise ActionFailure(400)
    value=request.app.state.services.actions
    if value is None:raise ActionFailure(503)
    return value


async def body(request):
    raw=await request.body()
    if len(raw)>131072:raise ActionFailure(400)
    def unique(rows):
        out={}
        for k,v in rows:
            if k in out:raise ValueError
            out[k]=v
        return out
    try:
        value=json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if type(value) is not dict:raise ValueError
        return value
    except (ValueError,UnicodeError):raise ActionFailure(400) from None


def output(request,value):
    request.state.action_authority_deadline=value.pop('_authority_expires_at')
    return value


@router.post('/api/v1/drafts/{draft_id}/confirm',status_code=202)
async def confirm(draft_id:str,request:Request):
    return output(request,await service(request).confirm(cookie(request,SESSION_COOKIE),browser_headers(request),draft_id,await body(request)))


@router.post('/api/v1/claims/{claim_id}/decisions',status_code=202)
async def decide(claim_id:str,request:Request):
    return output(request,await service(request).decide(cookie(request,SESSION_COOKIE),browser_headers(request),claim_id,await body(request)))


@router.get('/api/v1/actions/{operation_id}')
async def read(operation_id:str,request:Request):
    return output(request,await service(request).read(cookie(request,SESSION_COOKIE),operation_id))
