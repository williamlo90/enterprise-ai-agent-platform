import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
import httpx
from enterprise_ai_platform.modules.knowledge.service import KnowledgeService
from enterprise_ai_platform.modules.knowledge.domain import KnowledgeFailure
from enterprise_ai_platform.composition import ApplicationServices
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.main import create_app

ORIGIN='https://test.example'
def headers():return dict(origin=ORIGIN,**{'content-type':'application/json','x-csrf-token':'c'*43})
def service(repo=None):return KnowledgeService(repo or SimpleNamespace(call=AsyncMock()),ORIGIN,clock=lambda:10)
def row(**kw):return dict(authority_expires_at=20,**kw)
def hit():return dict(version_id=str(uuid4()),chunk_no=0,index_generation=1,access_revision=1,text_digest='a'*64,text='PRIVATE-FIRST-BUFFER')

@pytest.mark.parametrize('operation,values',[
 ('publish',{'expected_version':True,'key':str(uuid4())}),
 ('publish',{'expected_version':1,'key':'bad'}),
 ('withdraw',{'expected_version':1,'key':str(uuid4()),'tenant_id':str(uuid4())}),
 ('acl',{'expected_version':1,'key':str(uuid4()),'readers':None}),
 ('acl',{'expected_version':1,'key':str(uuid4()),'readers':['bad']}),
 ('acl',{'expected_version':1,'key':str(uuid4()),'readers':[str(uuid4())]*51}),
 ('unsupported',{'expected_version':1,'key':str(uuid4())}),
])
def test_invalid_lifecycle_commands_rejected_before_rpc(operation,values):
    instance=service()
    with pytest.raises(KnowledgeFailure):asyncio.run(instance.mutate('s'*43,headers(),operation,str(uuid4()),values))
    instance.repository.call.assert_not_called()


def test_acl_normalizes_explicit_readers_without_granting_management():
    a,b=sorted([str(uuid4()),str(uuid4())]);repo=SimpleNamespace(call=AsyncMock(return_value=row(access_revision=2)))
    asyncio.run(service(repo).mutate('s'*43,headers(),'acl',str(uuid4()),dict(expected_version=1,key=str(uuid4()),readers=[b,a,b])))
    payload=repo.call.call_args.args[2];assert payload['readers']==[a,b] and not {'role','permission'}&set(payload)

@pytest.mark.parametrize('query,language,limit',[
 ('','id',5),('x'*201,'en',5),('bad\nquery','id',5),('x','fr',5),('x','id',True),('x','en',11),('x','id',0),('\ud800','en',1),
])
def test_search_input_boundaries_do_not_hit_db(query,language,limit):
    instance=service()
    with pytest.raises(KnowledgeFailure):asyncio.run(instance.search('s'*43,query,language,limit))
    instance.repository.call.assert_not_called()


def test_search_discards_first_text_buffer_and_returns_reauthorized_subset():
    item=hit();repo=SimpleNamespace(call=AsyncMock(side_effect=[row(items=[item],search_mode='lexical_simple_v1'),row(items=[],search_mode='lexical_simple_v1')]))
    result=asyncio.run(service(repo).search('s'*43,'policy','en'))
    assert result==dict(items=[],search_mode='lexical_simple_v1') and 'PRIVATE' not in str(result)
    assert repo.call.call_args.args[1]=='search_recheck'
    assert repo.call.call_args.args[2]=={'items':[{k:v for k,v in item.items() if k!='text'}]}


def test_source_recheck_failure_never_releases_first_content():
    original=row(version_id=str(uuid4()),access_revision=2,index_generation=1,content_digest='b'*64,chunks=[dict(text='PRIVATE')])
    repo=SimpleNamespace(call=AsyncMock(side_effect=[original,KnowledgeFailure(404)]))
    with pytest.raises(KnowledgeFailure):asyncio.run(service(repo).source('s'*43,original['version_id']))
    assert repo.call.call_args.args[1]=='source_recheck' and 'chunks' not in repo.call.call_args.args[2]


def test_expiry_during_final_search_await_holds_all_output():
    repo=SimpleNamespace(call=AsyncMock(side_effect=[row(items=[]),dict(authority_expires_at=10,items=[])]))
    with pytest.raises(KnowledgeFailure) as error:asyncio.run(service(repo).search('s'*43,'policy','en'))
    assert error.value.status==401


def test_http_lifecycle_search_and_source_routes_reject_unsafe_input():
    instance=SimpleNamespace(mutate=AsyncMock(return_value={'active_version_id':str(uuid4())}),search=AsyncMock(return_value=dict(items=[],search_mode='lexical_simple_v1')),source=AsyncMock(return_value=dict(chunks=[])))
    async def run():
        app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=SimpleNamespace(),knowledge=instance))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
            h={**headers(),'cookie':'__Host-eai-session='+'s'*43};target=str(uuid4())
            response=await client.post('/api/v1/knowledge/versions/'+target+'/publish',json=dict(expected_version=1,key=str(uuid4())),headers=h)
            assert response.status_code==200 and response.headers['cache-control']=='no-store'
            response=await client.post('/api/v1/knowledge/versions/'+target+'/publish',json=dict(expected_version=True,key=str(uuid4())),headers=h)
            assert response.status_code==422 and response.headers['cache-control']=='no-store'
            for query in ('q=policy&language=en&q=second','q=policy&language=en&tenant=x','q=policy&language=en&limit=02','q=policy'):
                assert (await client.get('/api/v1/knowledge/search?'+query,headers=h)).status_code==400
            assert (await client.get('/api/v1/knowledge/search?q=policy&language=id&limit=2',headers=h)).status_code==200
            assert instance.search.call_args.args[1:]==('policy','id',2)
            assert (await client.get('/api/v1/knowledge/sources/'+target+'/content',headers={**h,'Range':'bytes=0-2'})).status_code==400
            assert (await client.get('/api/v1/knowledge/sources/'+target+'/content?url=foreign',headers=h)).status_code==400
            assert (await client.get('/api/v1/knowledge/sources/'+target+'/content',headers=h)).status_code==200
    asyncio.run(run())
