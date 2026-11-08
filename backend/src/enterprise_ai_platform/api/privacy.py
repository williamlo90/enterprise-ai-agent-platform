"""Operator configuration does not authorize access to subject content."""
import json
from fastapi import APIRouter,Request
from fastapi.responses import JSONResponse
from enterprise_ai_platform.api.identity import cookie,browser_headers,SESSION_COOKIE
from enterprise_ai_platform.modules.authorization.ports import AccessFailure

router=APIRouter()


async def invoke(request,operation,*,effect=False):
    request.state.authorization_required=True
    if request.scope.get('query_string'):raise AccessFailure(400)
    service=request.app.state.services.privacy_policy
    if service is None:raise AccessFailure(503)
    payload={}
    if operation=='publish':
        raw=await request.body()
        if len(raw)>65536:raise AccessFailure(400)
        def pairs(rows):
            out={}
            for k,v in rows:
                if k in out:raise ValueError
                out[k]=v
            return out
        try:
            payload=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            if type(payload) is not dict:raise ValueError
        except (ValueError,UnicodeError):raise AccessFailure(400) from None
    elif request.headers.get('content-length','0')!='0' or request.headers.get('transfer-encoding'):raise AccessFailure(400)
    method=service.effect_call if effect else service.call
    result=await method(cookie(request,SESSION_COOKIE),operation,payload,browser_headers(request))
    request.state.privacy_deadline=result.pop('_privacy_deadline');return result


@router.get('/api/v1/operator/privacy-policy')
async def current(request:Request):return await invoke(request,'current')


@router.post('/api/v1/operator/privacy-policy',status_code=201)
async def publish(request:Request):return await invoke(request,'publish')


@router.get('/api/v1/operator/privacy-effect-policy')
async def current_effect(request:Request):return await invoke(request,'current',effect=True)


@router.post('/api/v1/operator/privacy-effect-policy',status_code=201)
async def publish_effect(request:Request):return await invoke(request,'publish',effect=True)


@router.get('/api/v1/privacy/conversations/{conversation_id}/inventory')
async def inventory(conversation_id:str,request:Request):
    request.state.authorization_required=True
    if request.scope.get('query_string') or request.headers.get('content-length','0')!='0' or request.headers.get('transfer-encoding'):raise AccessFailure(400)
    service=request.app.state.services.privacy_policy
    if service is None:raise AccessFailure(503)
    result=await service.inventory(cookie(request,SESSION_COOKIE),conversation_id)
    request.state.privacy_deadline=result.pop('_privacy_deadline');return result


async def deletion_request(conversation_id,request,operation):
    request.state.authorization_required=True
    if request.scope.get('query_string'):raise AccessFailure(400)
    service=request.app.state.services.privacy_policy
    if service is None:raise AccessFailure(503)
    payload={}
    if operation in {'fence','release','export','delete'}:
        raw=await request.body()
        if len(raw)>4096:raise AccessFailure(400)
        def pairs(rows):
            out={}
            for k,v in rows:
                if k in out:raise ValueError
                out[k]=v
            return out
        try:
            payload=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            if type(payload) is not dict:raise ValueError
        except (ValueError,UnicodeError):raise AccessFailure(400) from None
    elif request.headers.get('content-length','0')!='0' or request.headers.get('transfer-encoding'):raise AccessFailure(400)
    result=await service.fence_call(cookie(request,SESSION_COOKIE),conversation_id,operation,payload,browser_headers(request))
    request.state.privacy_deadline=result.pop('_privacy_deadline');return result


@router.post('/api/v1/privacy/conversations/{conversation_id}/deletion',status_code=202)
async def request_deletion(conversation_id:str,request:Request):return await deletion_request(conversation_id,request,'fence')


@router.get('/api/v1/privacy/conversations/{conversation_id}/deletion')
async def deletion_status(conversation_id:str,request:Request):return await deletion_request(conversation_id,request,'fence_status')


@router.post('/api/v1/privacy/conversations/{conversation_id}/deletion/cancel',status_code=202)
async def cancel_deletion(conversation_id:str,request:Request):return await deletion_request(conversation_id,request,'release')


@router.post('/api/v1/privacy/conversations/{conversation_id}/export')
async def export_conversation(conversation_id:str,request:Request):
    result=await deletion_request(conversation_id,request,'export')
    return JSONResponse(result['document'],headers={'content-disposition':'attachment; filename="conversation-export.json"','cache-control':'no-store'})


@router.post('/api/v1/privacy/conversations/{conversation_id}/deletion/execute',status_code=202)
async def execute_deletion(conversation_id:str,request:Request):return await deletion_request(conversation_id,request,'delete')
