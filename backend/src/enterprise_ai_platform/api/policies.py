"""Versioned publisher commands; private eligibility is a separate router."""
import json
import re
from fastapi import APIRouter,Request
from typing import Literal
from enterprise_ai_platform.api.errors import error_response
from enterprise_ai_platform.api.identity import cookie,browser_headers,SESSION_COOKIE
from enterprise_ai_platform.modules.policies.ports import PolicyFailure
from enterprise_ai_platform.modules.delegation.ports import DelegationFailure

router=APIRouter();internal_router=APIRouter()

def service(request):
    request.state.authorization_required=True
    value=request.app.state.services.policies
    if value is None:raise PolicyFailure(503)
    if request.scope.get('query_string'):raise PolicyFailure(400)
    return value

async def body(request):
    raw=await request.body()
    if len(raw)>131072:raise PolicyFailure(400)
    def unique(items):
        values={}
        for key,value in items:
            if key in values:raise ValueError
            values[key]=value
        return values
    try:
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if type(value) is not dict:raise ValueError
        return value
    except (ValueError,UnicodeError):raise PolicyFailure(400) from None

@router.post('/api/v1/policy-packages/{package_id}/versions/{package_version}/{operation}')
async def mutate(package_id:str,package_version:str,operation:Literal['draft','prepare','dispatch','activate','supersede','retire','withdraw','conflict'],request:Request):
    instance=service(request)
    if not re.fullmatch('[1-9][0-9]{0,18}',package_version):raise PolicyFailure(400)
    values=await body(request)
    if {'package_id','package_version'}&values.keys():raise PolicyFailure(400)
    return await instance.mutate(cookie(request,SESSION_COOKIE),browser_headers(request),operation,
        dict(package_id=package_id,package_version=int(package_version),**values))

@internal_router.post('/internal/v1/policy-packages/{package_id}/eligibility')
async def eligibility(package_id:str,request:Request):
    try:
        instance=service(request);headers={}
        for key,value in request.scope.get('headers',[]):
            key=key.lower()
            if key in {b'authorization',b'x-eai-delegation',b'content-type',b'cookie'}:
                if key in headers:raise PolicyFailure(401)
                headers[key]=value.decode('latin-1')
        if b'cookie' in headers:raise PolicyFailure(401)
        authorization=headers.get(b'authorization','')
        if not authorization.startswith('Bearer '):raise PolicyFailure(401)
        if headers.get(b'content-type')!='application/json':raise PolicyFailure(400)
        values=await body(request)
        if type(values.get('policy')) is not dict or values['policy'].get('id')!=package_id:raise PolicyFailure(400)
        return await instance.eligibility(authorization[7:],headers.get(b'x-eai-delegation'),values)
    except (PolicyFailure,DelegationFailure) as error:return error_response(error.status,request.state.request_id)
