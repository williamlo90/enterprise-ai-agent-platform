"""Actual read service and private route; external actor source is controlled."""
import asyncio
import copy
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import httpx
import pytest
from reimbursement_service.modules.claims.service import ClaimsService
from reimbursement_service.modules.claims.ports import ClaimFailure
from reimbursement_service.modules.access.ports import VerifiedActor,request_hash
from reimbursement_service.modules.access.service import AccessService,Dispatcher,credential_digest
from reimbursement_service.main import create_app
from reimbursement_service.config import Settings,ConfigurationError


def uid(n):return str(UUID(int=n))


@pytest.fixture
def case():
    now=[int(time.time())];clock=lambda:now[0];issued=now[0]
    payload=dict(schema_version=1,action='claim.read',tenant_id=uid(1),actor_id=uid(2),operation_id=uid(3),object=dict(kind='claim',id=uid(4),version=1),data={})
    def actor(p):return VerifiedActor(p['actor_id'],p['tenant_id'],uid(8),1,p['action'],p['operation_id'],request_hash(p),issued,issued+60,False)
    view=dict(tenant_id=uid(1),id=uid(4),owner_id=uid(2),trip_id=uid(5),trip_date='2026-05-01',policy=dict(id=uid(6),version=1,digest='a'*64),
        state='pending_approval',version=3,revision=2,active_stage=2,reviewer_id=uid(7),
        receipts=[dict(id=uid(9),version=2,digest='b'*64)],financial={'private':'not returned'})
    repository=SimpleNamespace(read=AsyncMock(return_value=view))
    service=ClaimsService(repository,SimpleNamespace(),clock=clock)
    verifier=SimpleNamespace(verify=AsyncMock(side_effect=lambda token,p:actor(p)))
    dispatch=AccessService(verifier,Dispatcher('platform-dispatcher',credential_digest('a'*43),frozenset({'claim.read'})),clock=clock)
    class Database:
        async def open(self):pass
        async def close(self):pass
    factory=lambda _:(Database(),SimpleNamespace())
    app=create_app(Settings(database_url='fixture'),resource_factory=factory,claims=service,claim_dispatch=dispatch)
    headers={'Authorization':'Bearer '+'a'*43,'X-EAI-Delegation':'controlled','Content-Type':'application/json'}
    return SimpleNamespace(now=now,payload=payload,actor=actor,view=view,repository=repository,service=service,app=app,headers=headers,factory=factory,dispatch=dispatch)


def test_owner_access_is_minimal_current_revision_and_bound_to_original_actor(case):
    result=asyncio.run(case.service.access_check(case.payload,case.actor(case.payload)))
    assert set(result)=={'allow','tenant_id','actor_id','claim_id','claim_version','revision','receipts','operation_id','request_hash','checked_at','expires_at'}
    assert result['claim_version']==3 and result['revision']==2 and result['receipts']==case.view['receipts']
    assert result['request_hash']==request_hash(case.payload) and result['expires_at']==case.now[0]+60
    case.view['receipts'][0]['digest']='c'*64
    assert result['receipts'][0]['digest']=='b'*64


def test_decision_source_is_opt_in_and_only_exposes_current_reviewer_metadata(case):
    from reimbursement_service.api.claim_access import router
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(case.app),base_url='https://source.test') as client:
            path='/service/v1/claims/'+uid(4)+'/decision-source'
            assert (await client.post(path,json={'payload':case.payload},headers=case.headers)).status_code==404
            case.app.include_router(router(case.service,case.dispatch,decision_sources=True))
            assert (await client.post(path,json={'payload':case.payload},headers=case.headers)).status_code==403
            case.payload['actor_id']=uid(7)
            result=await client.post(path,json={'payload':case.payload},headers=case.headers)
            assert result.status_code==200
            claim=result.json()['claim']
            assert set(claim)=={'id','owner_id','trip_id','trip_date','state','version','revision','active_stage','reviewer_id','policy'}
            assert claim['version']==3 and claim['revision']==2 and claim['active_stage']==2
            assert 'financial' not in result.text and 'receipts' not in result.text
    asyncio.run(run())


def test_current_active_reviewer_allowed_old_or_future_reviewer_denied(case):
    case.payload['actor_id']=uid(7)
    assert asyncio.run(case.service.access_check(case.payload,case.actor(case.payload)))['allow'] is True
    case.view['reviewer_id']=uid(20)
    with pytest.raises(ClaimFailure):asyncio.run(case.service.access_check(case.payload,case.actor(case.payload)))


@pytest.mark.parametrize('change',[
    lambda c:c.view.update(tenant_id=uid(20)),lambda c:c.view.update(id=uid(20)),
    lambda c:c.view.update(version=True),lambda c:c.view.update(revision=0),
    lambda c:c.view.update(receipts=None),lambda c:c.view['receipts'].append(copy.deepcopy(c.view['receipts'][0])),
    lambda c:c.view['receipts'][0].update(version=True),lambda c:c.view['receipts'][0].update(digest='A'*64),
    lambda c:c.view['receipts'][0].update(allow=True),
])
def test_malformed_source_or_wrong_relation_fails_closed(case,change):
    change(case)
    with pytest.raises(ClaimFailure) as error:asyncio.run(case.service.access_check(case.payload,case.actor(case.payload)))
    assert error.value.status==503


def test_expiry_during_final_database_await_denies_receipt_set(case):
    async def late(*args):case.now[0]+=60;return case.view
    case.repository.read.side_effect=late
    with pytest.raises(ClaimFailure) as error:asyncio.run(case.service.access_check(case.payload,case.actor(case.payload)))
    assert error.value.status==401


def test_read_source_unavailable_does_not_fall_back_to_metadata_relation(case):
    case.repository.read.side_effect=ClaimFailure(503)
    with pytest.raises(ClaimFailure):asyncio.run(case.service.access_check(case.payload,case.actor(case.payload)))


def test_private_route_real_access_service_checks_caller_path_and_shape(case):
    async def run():
        async with case.app.router.lifespan_context(case.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=case.app),base_url='https://fixture.test') as client:
                path='/service/v1/claims/'+uid(4)+'/access-check'
                good=await client.post(path,json={'payload':case.payload},headers=case.headers)
                assert good.status_code==200 and good.json()['revision']==2
                assert good.headers['cache-control']=='no-store'
                case.repository.read.reset_mock()
                for url,body,headers in [
                    (path,{'payload':case.payload},dict(case.headers,Authorization='Bearer '+'b'*43)),
                    (path+'?role=approver',{'payload':case.payload},case.headers),
                    ('/service/v1/claims/'+uid(20)+'/access-check',{'payload':case.payload},case.headers),
                    (path,{'payload':case.payload,'allow':True},case.headers),
                    (path,{'payload':case.payload},dict(case.headers,Cookie='untrusted=yes')),
                ]:
                    denied=await client.post(url,json=body,headers=headers)
                    assert denied.status_code in {400,401,403}
                case.repository.read.assert_not_called()
                assert (await client.post('/service/v1/claims',json={},headers=case.headers)).status_code==404
    asyncio.run(run())


def test_unconfigured_access_route_is_absent_and_partial_configuration_rejected(case):
    with pytest.raises(ConfigurationError):create_app(Settings(database_url='fixture'),resource_factory=case.factory,claims=case.service)
    app=create_app(Settings(database_url='fixture'),resource_factory=case.factory)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://fixture.test') as client:
            assert (await client.post('/service/v1/claims/'+uid(4)+'/access-check',json={})).status_code==404
    asyncio.run(run())
