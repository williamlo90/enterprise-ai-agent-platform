"""Scoped browser administration. Route kind is trusted server input."""
import json
from urllib.parse import parse_qsl
from fastapi import APIRouter,Request
from enterprise_ai_platform.api.identity import cookie,browser_headers,SESSION_COOKIE
from enterprise_ai_platform.modules.authorization.ports import AccessFailure

router=APIRouter()
def service(request):
    request.state.authorization_required=True
    instance=request.app.state.services.administration
    if instance is None:raise AccessFailure(503)
    return instance

def params(request,listing=False):
    try:
        pairs=parse_qsl(request.scope.get('query_string',b'').decode('ascii'),strict_parsing=True,keep_blank_values=True,max_num_fields=2)
        values=dict(pairs)
        if len(values)!=len(pairs) or not listing and values or set(values)-{'after','limit'}:raise ValueError
        if listing:
            n=values.get('limit','20')
            if n not in tuple(str(v) for v in range(1,21)) or values.get('after')=='':raise ValueError
            return dict(after=values.get('after'),limit=int(n))
        return {}
    except (ValueError,UnicodeError):raise AccessFailure(400) from None

async def body(request):
    if request.scope.get('query_string'):raise AccessFailure(400)
    raw=await request.body()
    if len(raw)>262144:raise AccessFailure(413)
    def pairs(rows):
        out={}
        for k,v in rows:
            if k in out:raise ValueError
            out[k]=v
        return out
    try:
        value=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if type(value) is not dict:raise ValueError
        return value
    except (ValueError,UnicodeError):raise AccessFailure(400) from None

def output(request,value):
    request.state.administration_deadline=value.pop('_administration_deadline')
    return value

async def read(request,operation,values):
    if request.headers.get('content-length','0')!='0' or request.headers.get('transfer-encoding'):raise AccessFailure(400)
    return output(request,await service(request).read(cookie(request,SESSION_COOKIE),operation,values))
async def write(request,operation,extra=None):
    values=await body(request)
    if extra:
        if set(extra)&set(values):raise AccessFailure(400)
        values.update(extra)
    return output(request,await service(request).mutate(cookie(request,SESSION_COOKIE),browser_headers(request),operation,values))

@router.get('/api/v1/admin/memberships')
async def members(request:Request):return await read(request,'members',params(request,True))
@router.get('/api/v1/admin/memberships/{membership_id}')
async def member(membership_id:str,request:Request):
    params(request);return await read(request,'member',dict(id=membership_id))
@router.post('/api/v1/admin/memberships',status_code=201)
async def create_member(request:Request):return await write(request,'member_create')
@router.patch('/api/v1/admin/memberships/{membership_id}')
async def active(membership_id:str,request:Request):return await write(request,'member_active',dict(id=membership_id))
@router.post('/api/v1/admin/memberships/{membership_id}/roles')
async def role(membership_id:str,request:Request):return await write(request,'role',dict(id=membership_id))
@router.get('/api/v1/admin/config')
async def config(request:Request):return await read(request,'config',params(request))
@router.patch('/api/v1/admin/config')
async def replace_config(request:Request):return await write(request,'config_write')
@router.get('/api/v1/admin/config/catalog')
async def catalog(request:Request):return await read(request,'catalog',params(request))
@router.get('/api/v1/tenant/profile')
async def profile(request:Request):return await read(request,'profile',params(request))
@router.get('/api/v1/admin/scopes')
async def scopes(request:Request):return await read(request,'scopes',params(request,True))
@router.get('/api/v1/admin/policies')
async def policies(request:Request):return await read(request,'policies',params(request,True))
@router.get('/api/v1/admin/policies/{policy_id}/versions/{package_version}')
async def policy(policy_id:str,package_version:str,request:Request):
    import re
    params(request)
    if not re.fullmatch('[1-9][0-9]{0,17}',package_version):raise AccessFailure(400)
    return await read(request,'policy',dict(id=policy_id,package_version=int(package_version)))
@router.get('/api/v1/admin/audit')
async def audit(request:Request):return await read(request,'audit',params(request,True))
@router.get('/api/v1/operator/tenants')
async def tenants(request:Request):return await read(request,'tenants',params(request,True))
@router.post('/api/v1/operator/tenants',status_code=201)
async def provision(request:Request):return await write(request,'provision')
@router.get('/api/v1/operator/jobs')
@router.get('/api/v1/admin/jobs')
async def jobs(request:Request):return await read(request,'jobs',params(request,True))
