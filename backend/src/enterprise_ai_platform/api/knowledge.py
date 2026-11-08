from fastapi import APIRouter,Request
from typing import Literal
from urllib.parse import parse_qsl
from pydantic import Field
from enterprise_ai_platform.api.schemas import RequestModel
from enterprise_ai_platform.api.identity import cookie,browser_headers,SESSION_COOKIE
from enterprise_ai_platform.modules.knowledge.ports import KnowledgeFailure

router=APIRouter()
class CreateDocument(RequestModel):
    file_id:str
    key:str
    title:str=Field(min_length=1,max_length=200)
    readers:list[str]=Field(default_factory=list,max_length=50)
    valid_from:str|None=Field(default=None,pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
    valid_until:str|None=Field(default=None,pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
    audience_mode:Literal['specific_people','all_current_employees']|None=None
    selection_id:str|None=None
class CreateVersion(RequestModel):
    file_id:str
    key:str
    expected_version:int=Field(ge=1,lt=10**18)
    valid_from:str|None=Field(default=None,pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
    valid_until:str|None=Field(default=None,pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
class SelectionFilters(RequestModel):
    q:str|None=Field(default=None,max_length=200)
    department:str|None=Field(default=None,max_length=100)
    job_title:str|None=Field(default=None,max_length=100)
    group:str|None=Field(default=None,max_length=100)
class CreateSelection(RequestModel):
    key:str
    collection_id:str|None=None
    base_document_id:str|None=None
    expected_version:int|None=Field(default=None,strict=True,ge=1,lt=10**18)
    all_matching:bool=False
    retain_existing:bool=True
    filters:SelectionFilters=Field(default_factory=SelectionFilters)
    include_ids:list[str]=Field(default_factory=list,max_length=1000)
    exclude_ids:list[str]=Field(default_factory=list,max_length=1000)
class ReplaceAudience(RequestModel):
    audience_mode:Literal['specific_people','all_current_employees']
    selection_id:str|None=None
    expected_version:int=Field(strict=True,ge=1,lt=10**18)
    key:str
def service(request,*,query=False):
    request.state.authorization_required=True
    value=request.app.state.services.knowledge
    if value is None:raise KnowledgeFailure(503)
    if not query and request.scope.get('query_string'):raise KnowledgeFailure(400)
    return value
def workspace_query(request,*,catalog=False):
    try:
        allowed={'after','limit','q','collection'} if catalog else {'after','limit'}
        pairs=parse_qsl(request.scope.get('query_string',b'').decode('utf-8'),strict_parsing=True,keep_blank_values=True,max_num_fields=len(allowed),errors='strict')
        values=dict(pairs)
        if len(values)!=len(pairs) or set(values)-allowed:raise ValueError()
        raw=values.get('limit','20')
        if raw not in tuple(str(i) for i in range(1,51)):raise ValueError()
        result=dict(after=values.get('after'),limit=int(raw))
        if catalog:result.update(query=values.get('q'),collection=values.get('collection'))
        return result
    except (ValueError,UnicodeError):raise KnowledgeFailure(400) from None
@router.get('/api/v1/knowledge/documents/{document_id}/audience')
async def audience(document_id:str,request:Request):
    return await service(request,query=True).audience(cookie(request,SESSION_COOKIE),document_id,**workspace_query(request))
@router.patch('/api/v1/knowledge/documents/{document_id}/audience')
async def replace_audience(document_id:str,values:ReplaceAudience,request:Request):
    return await service(request).replace_audience(cookie(request,SESSION_COOKIE),browser_headers(request),document_id,values.model_dump())
@router.post('/api/v1/knowledge/selections',status_code=201)
async def create_selection(values:CreateSelection,request:Request):
    return await service(request).create_selection(cookie(request,SESSION_COOKIE),browser_headers(request),values.model_dump())
@router.get('/api/v1/knowledge/selections/{selection_id}')
async def selection(selection_id:str,request:Request):
    return await service(request,query=True).selection(cookie(request,SESSION_COOKIE),selection_id,**workspace_query(request))
@router.get('/api/v1/knowledge/documents')
async def catalog(request:Request):
    return await service(request,query=True).catalog(cookie(request,SESSION_COOKIE),**workspace_query(request,catalog=True))
@router.get('/api/v1/knowledge/documents/{document_id}')
async def detail(document_id:str,request:Request):
    return await service(request).detail(cookie(request,SESSION_COOKIE),document_id)
@router.get('/api/v1/knowledge/documents/{document_id}/versions')
async def versions(document_id:str,request:Request):
    return await service(request,query=True).versions(cookie(request,SESSION_COOKIE),document_id,**workspace_query(request))
@router.get('/api/v1/knowledge/documents/{document_id}/versions/{version_id}/preview')
async def preview(document_id:str,version_id:str,request:Request):
    if any(k.lower() in {b'range',b'if-none-match',b'if-modified-since'} for k,v in request.scope['headers']):raise KnowledgeFailure(400)
    return await service(request).preview(cookie(request,SESSION_COOKIE),document_id,version_id)
@router.post('/api/v1/knowledge/documents',status_code=202)
async def create(values:CreateDocument,request:Request):
    return await service(request).create(cookie(request,SESSION_COOKIE),browser_headers(request),values.model_dump())
@router.post('/api/v1/knowledge/documents/{document_id}/versions',status_code=202)
async def version(document_id:str,values:CreateVersion,request:Request):
    return await service(request).version(cookie(request,SESSION_COOKIE),browser_headers(request),document_id,values.model_dump())
@router.get('/api/v1/knowledge/documents/{document_id}/versions/{version_id}/status')
async def status(document_id:str,version_id:str,request:Request):
    return await service(request).status(cookie(request,SESSION_COOKIE),document_id,version_id)


class LifecycleCommand(RequestModel):
    expected_version:int=Field(ge=1,lt=10**18)
    key:str

class ReplaceACL(LifecycleCommand):
    readers:list[str]=Field(max_length=50)

@router.post('/api/v1/knowledge/versions/{version_id}/publish')
async def publish(version_id:str,values:LifecycleCommand,request:Request):
    return await service(request).mutate(cookie(request,SESSION_COOKIE),browser_headers(request),'publish',version_id,values.model_dump())

@router.post('/api/v1/knowledge/documents/{document_id}/withdraw')
async def withdraw(document_id:str,values:LifecycleCommand,request:Request):
    return await service(request).mutate(cookie(request,SESSION_COOKIE),browser_headers(request),'withdraw',document_id,values.model_dump())

@router.patch('/api/v1/knowledge/documents/{document_id}/acl')
async def acl(document_id:str,values:ReplaceACL,request:Request):
    return await service(request).mutate(cookie(request,SESSION_COOKIE),browser_headers(request),'acl',document_id,values.model_dump())

@router.get('/api/v1/knowledge/search')
async def search(request:Request):
    instance=service(request,query=True)
    try:
        pairs=parse_qsl(request.scope.get('query_string',b'').decode('utf-8'),strict_parsing=True,keep_blank_values=True,max_num_fields=3,errors='strict')
        values=dict(pairs)
        if len(pairs)!=len(values) or not {'q','language'}<=set(values) or set(values)-{'q','language','limit'}:raise ValueError()
        raw=values.get('limit','5')
        if raw not in tuple(str(i) for i in range(1,11)):raise ValueError()
    except (ValueError,UnicodeError):raise KnowledgeFailure(400) from None
    return await instance.search(cookie(request,SESSION_COOKIE),values['q'],values['language'],int(raw))

@router.get('/api/v1/knowledge/sources/{version_id}/content')
async def source(version_id:str,request:Request):
    if any(k.lower() in {b'range',b'if-none-match',b'if-modified-since'} for k,v in request.scope['headers']):raise KnowledgeFailure(400)
    return await service(request).source(cookie(request,SESSION_COOKIE),version_id)
