import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import httpx
import pytest
from enterprise_ai_platform.api.browser_context import read_target
from enterprise_ai_platform.composition import ApplicationServices
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.knowledge.service import KnowledgeService
from enterprise_ai_platform.modules.knowledge.domain import KnowledgeFailure

ORIGIN='https://audience.example'
def headers():return {'origin':ORIGIN,'content-type':'application/json','x-csrf-token':'c'*43}
def values():return dict(key=str(uuid4()),collection_id=None,base_document_id=None,expected_version=None,all_matching=False,filters={},include_ids=[],exclude_ids=[])

@pytest.mark.parametrize('changes',[
 {'tenant_id':str(uuid4())},{'all_matching':1},{'retain_existing':1},{'key':'bad'},{'collection_id':'bad'},
 {'base_document_id':str(uuid4())},{'expected_version':1},{'filters':{'role':'employee'}},
 {'filters':{'q':' '}},{'filters':{'q':'\ud800'}},{'filters':{'group':'x'*101}},
 {'include_ids':['bad']},{'exclude_ids':[True]},{'include_ids':[str(uuid4())]*1001},
])
def test_selection_rejects_unsafe_authority_filter_and_payload_without_db(changes):
 repo=SimpleNamespace(call=AsyncMock())
 with pytest.raises(KnowledgeFailure):asyncio.run(KnowledgeService(repo,ORIGIN).create_selection('s'*43,headers(),{**values(),**changes}))
 repo.call.assert_not_called()

def test_selection_canonicalizes_only_individuals_and_filters_without_inventing_all_matching():
 repo=SimpleNamespace(call=AsyncMock(return_value={'authority_expires_at':20,'selected_count':2}));pid=str(uuid4());material={**values(),'include_ids':[pid,pid],'filters':{'department':' Finance ','job_title':'Manager'}}
 assert asyncio.run(KnowledgeService(repo,ORIGIN,clock=lambda:10).create_selection('s'*43,headers(),material))==dict(selected_count=2)
 _,operation,payload=repo.call.call_args.args
 assert operation=='selection_create' and payload['all_matching'] is False and payload['include_ids']==[pid]
 assert payload['filters']==dict(query=None,department='Finance',job_title='Manager',group=None)

@pytest.mark.parametrize('change',[
 {'audience_mode':'everyone'},{'audience_mode':'specific_people','selection_id':None},
 {'audience_mode':'all_current_employees','selection_id':str(uuid4())},{'expected_version':True},{'key':'bad'},
 {'actor_id':str(uuid4())},
])
def test_audience_mutation_is_exact_mode_cas_key_material(change):
 repo=SimpleNamespace(call=AsyncMock());material=dict(audience_mode='all_current_employees',selection_id=None,expected_version=1,key=str(uuid4()));material.update(change)
 with pytest.raises(KnowledgeFailure):asyncio.run(KnowledgeService(repo,ORIGIN).replace_audience('s'*43,headers(),str(uuid4()),material))
 repo.call.assert_not_called()

@pytest.mark.parametrize('audience',[
 {'audience_mode':'all_current_employees','readers':[str(uuid4())]},
 {'audience_mode':None,'selection_id':str(uuid4())},
 {'audience_mode':'all_current_employees','selection_id':str(uuid4())},
 {'audience_mode':'specific_people','selection_id':str(uuid4()),'readers':[str(uuid4())]},
])
def test_admission_does_not_mix_named_legacy_readers_with_selection_or_dynamic_mode(audience):
 repo=SimpleNamespace(call=AsyncMock());material=dict(file_id=str(uuid4()),key=str(uuid4()),title='Fictional',readers=[]);material.update(audience)
 with pytest.raises(KnowledgeFailure):asyncio.run(KnowledgeService(repo,ORIGIN).create('s'*43,headers(),material))
 repo.call.assert_not_called()

def test_audience_and_selection_review_map_to_server_final_attestation():
 assert read_target('/api/v1/knowledge/documents/x/audience')==('knowledge','audience')
 assert read_target('/api/v1/knowledge/selections/x')==('knowledge','selection')

def test_http_selection_input_is_strict_and_early_private_failures_remain_no_store():
 instance=SimpleNamespace(create_selection=AsyncMock(return_value={}),replace_audience=AsyncMock(return_value={}))
 async def run():
  settings=Settings(env='test',host='127.0.0.1',port=8000)
  app=create_app(settings,services=ApplicationServices(health=SimpleNamespace(),knowledge=instance))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
   h={**headers(),'cookie':'__Host-eai-session='+'s'*43}
   assert (await client.post('/api/v1/knowledge/selections',json={'key':str(uuid4())},headers=h)).status_code==201
   for extra in ({'all_matching':1},{'filters':{'tenant_id':'x'}},{'include_ids':['x']*1001}):
    assert (await client.post('/api/v1/knowledge/selections',json={'key':str(uuid4()),**extra},headers=h)).status_code==422
  protected=create_app(settings.model_copy(update={'browser_context_enabled':True}),services=ApplicationServices(health=SimpleNamespace()))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=protected),base_url=ORIGIN) as client:
   response=await client.get('/api/v1/organization/members')
   assert response.status_code==400 and response.headers['cache-control']=='no-store'
 asyncio.run(run())
