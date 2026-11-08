import json
from fastapi import APIRouter,Request
from enterprise_ai_platform.api.identity import cookie,browser_headers,SESSION_COOKIE
from enterprise_ai_platform.modules.drafts.ports import DraftFailure

router=APIRouter()


def service(request):
    request.state.authorization_required=True
    if request.scope.get('query_string'):raise DraftFailure(400)
    value=request.app.state.services.drafts
    if value is None:raise DraftFailure(503)
    return value


async def body(request):
    raw=await request.body()
    if len(raw)>131072:raise DraftFailure(400)
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result:raise ValueError
            result[k]=v
        return result
    try:
        value=json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if type(value) is not dict:raise ValueError
        return value
    except (ValueError,UnicodeError):raise DraftFailure(400) from None


def output(request,value):
    request.state.draft_authority_deadline=value.pop('_authority_expires_at')
    return value


@router.post('/api/v1/drafts')
async def create(request:Request):
    value=await service(request).create(cookie(request,SESSION_COOKIE),browser_headers(request),await body(request))
    return output(request,value)


@router.get('/api/v1/drafts/{draft_id}')
async def read(draft_id:str,request:Request):
    return output(request,await service(request).read(cookie(request,SESSION_COOKIE),draft_id))


@router.patch('/api/v1/drafts/{draft_id}')
async def replace(draft_id:str,request:Request):
    return output(request,await service(request).replace(cookie(request,SESSION_COOKIE),browser_headers(request),draft_id,await body(request)))


@router.post('/api/v1/drafts/{draft_id}/preview')
async def preview(draft_id:str,request:Request):
    return output(request,await service(request).preview(cookie(request,SESSION_COOKIE),browser_headers(request),draft_id,await body(request)))
