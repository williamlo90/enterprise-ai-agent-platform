"""Private browser workflow views. Existing explicit UI mutations stay shared."""
from urllib.parse import parse_qsl
from fastapi import APIRouter,Request
from enterprise_ai_platform.api.identity import cookie,SESSION_COOKIE
from enterprise_ai_platform.modules.actions.ports import ActionFailure

router=APIRouter()
def service(request):
    request.state.authorization_required=True
    if request.headers.get('content-length','0')!='0' or request.headers.get('transfer-encoding'):raise ActionFailure(400)
    value=request.app.state.services.workspace
    if value is None:raise ActionFailure(503)
    return value

def filters(request,detail=False):
    try:
        pairs=parse_qsl(request.scope.get('query_string',b'').decode('ascii'),strict_parsing=True,keep_blank_values=True,max_num_fields=2,errors='strict');values=dict(pairs)
        if len(values)!=len(pairs) or set(values)-{'after','limit'} or (detail and values):raise ValueError
        limit=values.get('limit','20')
        if limit not in tuple(str(n) for n in range(1,21)):raise ValueError
        after=values.get('after')
        if after=='':raise ValueError
        return dict(after=after,limit=int(limit))
    except (ValueError,UnicodeError):raise ActionFailure(400) from None

def output(request,value):
    request.state.workspace_authority_deadline=value.pop('_workspace_authority_deadline')
    return value

@router.get('/api/v1/drafts')
async def drafts(request:Request):return output(request,await service(request).owned(cookie(request,SESSION_COOKIE),'drafts',**filters(request)))
@router.get('/api/v1/actions')
async def actions(request:Request):return output(request,await service(request).owned(cookie(request,SESSION_COOKIE),'actions',**filters(request)))
@router.get('/api/v1/trips/{trip_id}')
async def trip(trip_id:str,request:Request):
    filters(request,True);return output(request,await service(request).trip(cookie(request,SESSION_COOKIE),trip_id))
@router.get('/api/v1/claims')
async def mine(request:Request):return output(request,await service(request).read(cookie(request,SESSION_COOKIE),'mine',**filters(request)))
@router.get('/api/v1/claims/{claim_id}')
async def claim(claim_id:str,request:Request):
    filters(request,True);return output(request,await service(request).read(cookie(request,SESSION_COOKIE),'mine',claim_id=claim_id,limit=1))
@router.get('/api/v1/approvals')
async def approvals(request:Request):return output(request,await service(request).read(cookie(request,SESSION_COOKIE),'approvals',**filters(request)))
@router.get('/api/v1/approvals/{claim_id}')
async def approval(claim_id:str,request:Request):
    filters(request,True);return output(request,await service(request).read(cookie(request,SESSION_COOKIE),'approvals',claim_id=claim_id,limit=1))

@router.get('/api/v1/drafts/{draft_id}/rules')
async def rules(draft_id:str,request:Request):
    try:
        pairs=parse_qsl(request.scope.get('query_string',b'').decode('ascii'),strict_parsing=True,keep_blank_values=True,max_num_fields=1)
        if len(pairs)!=1 or pairs[0][0]!='material_hash':raise ValueError
    except (ValueError,UnicodeError):raise ActionFailure(400) from None
    return output(request,await service(request).rules(cookie(request,SESSION_COOKIE),draft_id,pairs[0][1]))
