from typing import Literal
from urllib.parse import parse_qsl
from fastapi import APIRouter,Request
from pydantic import Field
from enterprise_ai_platform.api.schemas import RequestModel
from enterprise_ai_platform.api.identity import cookie,browser_headers,SESSION_COOKIE
from enterprise_ai_platform.modules.conversations.ports import ConversationFailure

router=APIRouter()
class CreateConversation(RequestModel):
    language:Literal['id','en']
    key:str
class HumanMessage(RequestModel):
    text:str=Field(min_length=1,max_length=4096)
    client_message_id:str
class RenameConversation(RequestModel):
    name:str=Field(min_length=1,max_length=4096)
    expected_version:int=Field(strict=True,ge=1,lt=10**18)
    key:str
class Feedback(RequestModel):
    key:str
    rating:Literal['up','down']
    reason:str|None=Field(default=None,max_length=1000)
class ResumeRun(RequestModel):
    expected_version:int=Field(strict=True,ge=1,lt=10**18)
def service(request,*,query=False):
    request.state.authorization_required=True
    value=request.app.state.services.conversations
    if value is None:raise ConversationFailure(503)
    if not query and request.scope.get('query_string'):raise ConversationFailure(400)
    return value
def pagination(request,history=False):
    try:
        pairs=parse_qsl(request.scope.get('query_string',b'').decode('ascii'),strict_parsing=True,keep_blank_values=True,max_num_fields=2,errors='strict')
        values=dict(pairs)
        if len(values)!=len(pairs) or set(values)-{'after','limit'}:raise ValueError()
        limit=values.get('limit','20' if history else '10')
        if limit not in tuple(str(n) for n in range(1,21)):raise ValueError()
        after=values.get('after','0' if history else None)
        if history:
            if not after or not after.isascii() or not after.isdecimal() or str(int(after))!=after or len(after)>18:raise ValueError()
            after=int(after)
        return after,int(limit)
    except (ValueError,UnicodeError):raise ConversationFailure(400) from None
@router.post('/api/v1/conversations',status_code=201)
async def create(values:CreateConversation,request:Request):return await service(request).create(cookie(request,SESSION_COOKIE),browser_headers(request),values.model_dump())
@router.get('/api/v1/conversations')
async def conversations(request:Request):
    instance=service(request,query=True);after,limit=pagination(request)
    return await instance.list(cookie(request,SESSION_COOKIE),after,limit)
@router.post('/api/v1/conversations/{conversation_id}/messages',status_code=202)
async def append(conversation_id:str,values:HumanMessage,request:Request):return await service(request).append(cookie(request,SESSION_COOKIE),browser_headers(request),conversation_id,values.model_dump())
@router.patch('/api/v1/conversations/{conversation_id}/name')
async def rename(conversation_id:str,values:RenameConversation,request:Request):return await service(request).rename(cookie(request,SESSION_COOKIE),browser_headers(request),conversation_id,values.model_dump())
@router.get('/api/v1/conversations/{conversation_id}/messages')
async def history(conversation_id:str,request:Request):
    instance=service(request,query=True);after,limit=pagination(request,True)
    return await instance.history(cookie(request,SESSION_COOKIE),conversation_id,after,limit)
@router.post('/api/v1/messages/{message_id}/feedback')
async def feedback(message_id:str,values:Feedback,request:Request):return await service(request).feedback(cookie(request,SESSION_COOKIE),browser_headers(request),message_id,values.model_dump())
@router.get('/api/v1/runs/{run_id}')
async def run(run_id:str,request:Request):return await service(request).run(cookie(request,SESSION_COOKIE),run_id)
@router.post('/api/v1/runs/{run_id}/resume',status_code=202)
async def resume(run_id:str,values:ResumeRun,request:Request):return await service(request).run(cookie(request,SESSION_COOKIE),run_id,headers=browser_headers(request),expected_version=values.expected_version)
@router.get('/api/v1/runs/{run_id}/diagnostics')
async def diagnostics(run_id:str,request:Request):return await service(request).diagnostics(cookie(request,SESSION_COOKIE),run_id)
@router.post('/api/v1/runs/{run_id}/recover')
async def recover(run_id:str,values:ResumeRun,request:Request):return await service(request).recover(cookie(request,SESSION_COOKIE),browser_headers(request),run_id,values.expected_version)

class AssistedMessage(HumanMessage):
    selection:dict

def assisted_service(request):
    settings=request.app.state.settings
    if not getattr(settings,'flow_enabled',False) or not getattr(settings,'drafts_enabled',False):raise ConversationFailure(503)
    return service(request)

@router.post('/api/v1/conversations/{conversation_id}/workflow',status_code=202)
async def assisted(conversation_id:str,values:AssistedMessage,request:Request):
    return await assisted_service(request).assisted(cookie(request,SESSION_COOKIE),browser_headers(request),conversation_id,values.model_dump())

@router.get('/api/v1/runs/{run_id}/workflow')
async def assisted_read(run_id:str,request:Request):
    return await assisted_service(request).assisted_read(cookie(request,SESSION_COOKIE),run_id)
