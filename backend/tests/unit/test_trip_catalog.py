import asyncio
import base64
import copy
import json
from pathlib import Path
import sys
import httpx
import pytest
from enterprise_ai_platform.composition import ApplicationServices
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.delegation_check import SigningFixture,ISSUER,AUDIENCE,CALLER
from enterprise_ai_platform.integrations.reimbursement.policies import PolicyTarget
from enterprise_ai_platform.integrations.reimbursement.trips import TripCatalogReader
from enterprise_ai_platform.integrations.security.catalog import RSACatalogSigner
from enterprise_ai_platform.integrations.security.delegation import RSADelegationVerifier
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.delegation.domain import catalog_hash
from enterprise_ai_platform.modules.trips.domain import TripFailure,page
from enterprise_ai_platform.modules.trips.service import TripsService
from test_policies import uid
sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'reference_service/src'))
from reimbursement_service.modules.trips.domain import listing_digest

NOW=1800000000
CONTEXT=dict(tenant_id=uid(1),actor_id=uid(2),membership_id=uid(3),session_id=uid(4),access_revision=1,authority_expires_at=NOW+30)


def summary(n=10):return dict(id=uid(n),start_date='2026-01-01',end_date='2026-01-03',state='completed',employee_ref='Fictional employee')
def result():return dict(items=[summary()],next_cursor=None)


def reader(handler,clock=lambda:NOW):
    key=SigningFixture()
    value=TripCatalogReader(PolicyTarget('https://reference.example.test','r'*43),RSACatalogSigner(key.private,'current',ISSUER,AUDIENCE,CALLER),transport=httpx.MockTransport(handler),clock=clock)
    return value,key


def test_exact_collection_v2_signature_hash_and_independent_REF04_filter_binding():
    holder={};seen=[]
    def handle(request):
        assert request.method=='GET' and request.url.path=='/service/v1/trips'
        assert dict(request.url.params)==dict(after=uid(5),limit='2')
        payload=json.loads(base64.urlsafe_b64decode(request.headers['x-eai-catalog-payload']+'='*((-len(request.headers['x-eai-catalog-payload']))%4)))
        claims=RSADelegationVerifier().verify(request.headers['x-eai-delegation'],holder['key'].jwk)
        assert payload['object']==dict(kind='trip_collection',id=None,version=1)
        assert payload['data']==dict(after=uid(5),limit=2,request_digest=listing_digest(uid(5),2))
        assert claims['canonical_version']==2 and claims['object_id'] is None and claims['human_receipt_id'] is None
        assert claims['request_hash']==catalog_hash(payload) and claims['sub']==CONTEXT['actor_id'] and claims['exp']==NOW+30
        assert request.headers['x-eai-operation-id']==claims['operation_id']==payload['operation_id']
        assert 'cookie' not in request.headers and 'idempotency-key' not in request.headers
        seen.append(claims['jti']);return httpx.Response(200,json=result())
    value,key=reader(handle);holder['key']=key
    async def run():
        try:
            assert await value.listing(CONTEXT,uid(5),2)==(result(),NOW+30)
            await value.listing(CONTEXT,uid(5),2)
        finally:await value.close()
    asyncio.run(run());assert len(set(seen))==2


@pytest.mark.parametrize('change',['extra','items_content','duplicate','unsorted','bad_cursor','empty_cursor','foreign_cursor','boolean_limit','over_limit','bad_date','control_employee'])
def test_invalid_list_or_content_leak_never_reaches_caller(change):
    value=result();after=None;limit=2
    if change=='extra':value['total']=100
    elif change=='items_content':value['items'][0]['items']=[{'amount':100}]
    elif change=='duplicate':value['items']*=2
    elif change=='unsorted':value['items']=[summary(11),summary(10)]
    elif change=='bad_cursor':value['next_cursor']=uid(99)
    elif change=='empty_cursor':value=dict(items=[],next_cursor=uid(10))
    elif change=='foreign_cursor':after=uid(11)
    elif change=='boolean_limit':limit=True
    elif change=='over_limit':limit=1;value['items'].append(summary(11))
    elif change=='bad_date':value['items'][0]['end_date']='2025-01-01'
    else:value['items'][0]['employee_ref']='secret\nline'
    with pytest.raises(TripFailure):page(value,after,limit)


@pytest.mark.parametrize('mode',['redirect','timeout','duplicate_json','oversize','content_type','expired'])
def test_transport_faults_fail_closed_without_redirect_or_retry(mode):
    calls=[]
    def handle(request):
        calls.append(1)
        if mode=='redirect':return httpx.Response(307,headers={'location':'https://foreign.example.test'})
        if mode=='timeout':raise httpx.ReadTimeout('sensitive transport detail')
        if mode=='duplicate_json':return httpx.Response(200,content=b'{"items":[],"items":[],"next_cursor":null}',headers={'content-type':'application/json'})
        if mode=='oversize':return httpx.Response(200,content=b' '*32769,headers={'content-type':'application/json'})
        return httpx.Response(200,json=result(),headers={'content-type':'text/html'})
    value,_=reader(handle);context={**CONTEXT,'authority_expires_at':NOW} if mode=='expired' else CONTEXT
    async def run():
        try:
            with pytest.raises(TripFailure) as error:await value.listing(context,None,2)
            assert error.value.status==503 and 'sensitive' not in str(error.value)
        finally:await value.close()
    asyncio.run(run());assert len(calls)==(0 if mode=='expired' else 1)


@pytest.mark.parametrize('mode',['ok','revoked','changed_epoch','expired_after_read'])
def test_service_rechecks_same_current_authority_after_network_await(mode):
    calls=[];clock=[NOW]
    class Repo:
        async def context(self,token,operation):
            assert token!='s'*43;calls.append(operation)
            if operation=='list_recheck':
                if mode=='revoked':raise TripFailure(403)
                if mode=='changed_epoch':return {**CONTEXT,'access_revision':2}
            return copy.deepcopy(CONTEXT)
    class Reader:
        async def listing(self,context,after,limit):
            assert calls==['context']
            if mode=='expired_after_read':clock[0]=NOW+31
            return result(),NOW+30
    service=TripsService(Repo(),Reader(),clock=lambda:clock[0])
    if mode=='ok':assert asyncio.run(service.listing('s'*43,limit=2))==result()
    else:
        with pytest.raises(TripFailure):asyncio.run(service.listing('s'*43,limit=2))
    assert calls==(['context'] if mode=='expired_after_read' else ['context','list_recheck'])


def test_public_query_contract_and_early_errors_always_no_store():
    class Service:
        async def listing(self,session,**values):assert values==dict(after=None,limit=2);return result()
    async def run():
        app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=None,trips=Service()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://platform.test') as client:
            assert (await client.get('/api/v1/trips?limit=2')).status_code==200
            for query in ('limit=01','limit=true','limit=2&limit=3','tenant_id='+uid(99),'after=','limit=0'):
                reply=await client.get('/api/v1/trips?'+query)
                assert reply.status_code==400 and reply.headers['cache-control']=='no-store'
    asyncio.run(run())
