from urllib.parse import parse_qsl
from fastapi import APIRouter,Request
from pydantic import Field
from enterprise_ai_platform.api.schemas import RequestModel
from enterprise_ai_platform.api.identity import cookie,browser_headers,SESSION_COOKIE
from enterprise_ai_platform.modules.organization.ports import OrganizationFailure

router=APIRouter()
class OrganizationReplacement(RequestModel):
    display_name:str|None=Field(max_length=200)
    department:str|None=Field(max_length=100)
    job_title:str|None=Field(max_length=100)
    groups:list[str]=Field(max_length=32)
    expected_version:int=Field(strict=True,ge=1,lt=10**18)
    key:str

def service(request,*,query=False):
    request.state.authorization_required=True
    value=request.app.state.services.organization
    if value is None:raise OrganizationFailure(503)
    if not query and request.scope.get('query_string'):raise OrganizationFailure(400)
    return value

def parameters(request,*,picker=False,facets=False,collections=False):
    try:
        allowed={'after','limit'} if collections else {'q','department','job_title','group','after','limit'}
        if picker:allowed.add('collection')
        if facets:allowed.add('field')
        pairs=parse_qsl(request.scope.get('query_string',b'').decode('utf-8'),strict_parsing=True,keep_blank_values=True,max_num_fields=len(allowed),errors='strict')
        values=dict(pairs)
        if len(values)!=len(pairs) or set(values)-allowed or any(not value for value in values.values()):raise ValueError()
        raw=values.get('limit','20')
        if raw not in tuple(str(n) for n in range(1,51)) or facets and 'field' not in values:raise ValueError()
        result=dict(values);result['limit']=int(raw)
        if 'q' in result:result['query']=result.pop('q')
        return result
    except (ValueError,UnicodeError):raise OrganizationFailure(400) from None

@router.get('/api/v1/organization/members')
async def members(request:Request):return await service(request,query=True).directory(cookie(request,SESSION_COOKIE),mode='admin',**parameters(request))
@router.get('/api/v1/organization/facets')
async def admin_facets(request:Request):return await service(request,query=True).facets(cookie(request,SESSION_COOKIE),mode='admin',**parameters(request,facets=True))
@router.get('/api/v1/organization/members/{membership_id}')
async def member(membership_id:str,request:Request):return await service(request).member(cookie(request,SESSION_COOKIE),membership_id)
@router.patch('/api/v1/organization/members/{membership_id}')
async def replace(membership_id:str,values:OrganizationReplacement,request:Request):return await service(request).replace(cookie(request,SESSION_COOKIE),browser_headers(request),membership_id,values.model_dump())
@router.get('/api/v1/knowledge/directory')
async def directory(request:Request):return await service(request,query=True).directory(cookie(request,SESSION_COOKIE),mode='picker',**parameters(request,picker=True))
@router.get('/api/v1/knowledge/directory/facets')
async def picker_facets(request:Request):return await service(request,query=True).facets(cookie(request,SESSION_COOKIE),mode='picker',**parameters(request,picker=True,facets=True))
@router.get('/api/v1/knowledge/collections')
async def collections(request:Request):return await service(request,query=True).collections(cookie(request,SESSION_COOKIE),**parameters(request,collections=True))
