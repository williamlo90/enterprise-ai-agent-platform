import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import httpx
import pytest
from pydantic import ValidationError
from enterprise_ai_platform.api.browser_context import read_target
from enterprise_ai_platform.composition import ApplicationServices
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.organization.service import OrganizationService
from enterprise_ai_platform.modules.organization.ports import OrganizationFailure

ORIGIN='https://organization.example'
def values():return dict(display_name=' Budi ',department='Finance',job_title='Manager',groups=[' Team B ','Team A','Team A'],expected_version=1,key=str(uuid4()))
def headers():return {'origin':ORIGIN,'content-type':'application/json','x-csrf-token':'c'*43}

@pytest.mark.parametrize('change',[
 {'tenant_id':str(uuid4())},{'role':'tenant_admin'},{'expected_version':True},{'expected_version':0},
 {'key':'bad'},{'display_name':'x'*201},{'department':'界'*134},{'job_title':'\ud800'},{'groups':['bad\nname']},
 {'groups':[None]},{'groups':['x']*33},{'groups':'group'},{'display_name':''},{'department':'  '},
])
def test_profile_rejects_unsafe_material_before_repository(change):
 repo=SimpleNamespace(call=AsyncMock());service=OrganizationService(repo,ORIGIN)
 with pytest.raises(OrganizationFailure):asyncio.run(service.replace('s'*43,headers(),str(uuid4()),{**values(),**change}))
 repo.call.assert_not_called()

def test_profile_canonicalizes_local_data_and_preserves_null_override():
 repo=SimpleNamespace(call=AsyncMock(side_effect=[dict(authority_expires_at=20,organization_version=2),dict(authority_expires_at=20,organization_version=3)]));service=OrganizationService(repo,ORIGIN,clock=lambda:10)
 material=values();mid=str(uuid4())
 assert asyncio.run(service.replace('s'*43,headers(),mid,material))==dict(organization_version=2)
 payload=repo.call.call_args.args[2]
 assert payload['display_name']=='Budi' and payload['groups']==['Team A','Team B'] and 'tenant_id' not in payload
 asyncio.run(service.replace('s'*43,headers(),mid,{**material,'display_name':None,'groups':[]}))
 assert repo.call.call_args.args[2]['display_name'] is None and repo.call.call_args.args[2]['groups']==[]

@pytest.mark.parametrize('filters',[{'mode':'employee'},{'mode':'admin','collection':'tenant'},{'mode':'picker','collection':'foreign'}, {'mode':'picker','limit':True},{'mode':'admin','after':'bad'},{'mode':'picker','query':' '},{'mode':'picker','group':'x'*101}])
def test_directory_filters_fail_closed(filters):
 repo=SimpleNamespace(call=AsyncMock())
 with pytest.raises(OrganizationFailure):asyncio.run(OrganizationService(repo,ORIGIN).directory('s'*43,**filters))
 repo.call.assert_not_called()

def test_missing_csrf_or_expired_authority_releases_no_profile():
 repo=SimpleNamespace(call=AsyncMock(return_value=dict(authority_expires_at=10,display_name='private')));service=OrganizationService(repo,ORIGIN,clock=lambda:10)
 with pytest.raises(OrganizationFailure):asyncio.run(service.replace('s'*43,{},str(uuid4()),values()))
 repo.call.assert_not_called()
 with pytest.raises(OrganizationFailure):asyncio.run(service.member('s'*43,str(uuid4())))

@pytest.mark.parametrize('path,operation',[
 ('/organization/members','directory'),('/organization/members/x','member'),('/organization/facets','facets'),
 ('/knowledge/directory','directory'),('/knowledge/directory/facets','facets'),('/knowledge/collections','collections'),
])
def test_all_private_organization_gets_have_explicit_final_attestation(path,operation):
 assert read_target('/api/v1'+path)==('organization',operation)

def test_http_strict_query_model_authority_and_optional_feature_disabled():
 instance=SimpleNamespace(directory=AsyncMock(return_value=dict(items=[])),facets=AsyncMock(return_value=dict(items=[])),collections=AsyncMock(return_value=dict(items=[])),member=AsyncMock(return_value={}),replace=AsyncMock(return_value={}))
 async def run():
  app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=SimpleNamespace(),organization=instance))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
   base='/api/v1/organization/members';h={**headers(),'cookie':'__Host-eai-session='+'s'*43}
   for query in ('?tenant_id=x','?mode=admin','?limit=02','?q=a&q=b','?collection=tenant','?q='):
    assert (await client.get(base+query,headers=h)).status_code==400
   assert (await client.get('/api/v1/knowledge/directory?department=Finance&job_title=Manager',headers=h)).status_code==200
   assert instance.directory.call_args.kwargs==dict(mode='picker',department='Finance',job_title='Manager',limit=20)
   assert (await client.get('/api/v1/knowledge/directory/facets',headers=h)).status_code==400
   assert (await client.get('/api/v1/knowledge/directory/facets?field=group&limit=1',headers=h)).status_code==200
   assert (await client.patch(base+'/'+str(uuid4()),json={**values(),'active':False},headers=h)).status_code==422
   assert (await client.patch(base+'/'+str(uuid4()),json=values(),headers=h)).status_code==200
  disabled=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=SimpleNamespace()))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=disabled),base_url=ORIGIN) as client:assert (await client.get(base)).status_code==404
 asyncio.run(run())

def test_initial_max_metadata_and_page_envelope_is_bounded_utf8_json():
 groups=[str(n)+'😀'*98 for n in range(32)]
 member=dict(membership_id=str(uuid4()),principal_id=str(uuid4()),display_name='😀'*200,local_display_name='😀'*200,department='😀'*100,job_title='😀'*100,groups=groups,organization_version=2,membership_active=True,principal_active=True)
 page=dict(items=[member]*50,next_after=str(uuid4()))
 payload={**values(),'display_name':member['display_name'],'department':member['department'],'job_title':member['job_title'],'groups':groups}
 assert len(json.dumps(page,ensure_ascii=False).encode('utf-8'))<1_048_576
 assert len(json.dumps(payload,ensure_ascii=False).encode('utf-8'))<16384

def test_organization_feature_default_off_and_requires_database_and_auth():
 assert Settings(env='test',host='127.0.0.1',port=8000).organization_enabled is False
 for value in (True,'true','1'):
  with pytest.raises(ValidationError):Settings(env='test',host='127.0.0.1',port=8000,organization_enabled=value)
