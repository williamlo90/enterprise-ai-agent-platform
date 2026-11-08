import asyncio,copy,time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.privacy.domain import Category,REQUIRED
from enterprise_ai_platform.modules.privacy.registry import PrivacyRegistryService
from enterprise_ai_platform.modules.authorization.ports import AccessFailure
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.api.browser_context import read_target,mutation_target


def payload():
    return dict(key=str(uuid4()),revision=1,expires_at=2000,rules=[dict(category=c.value,retention_seconds=10,regions=['ap-southeast-1'],stores=sorted(REQUIRED[c]),provider_retention_seconds=0,training_allowed=False) for c in Category])


HEADERS={'origin':'https://app.example.test','content-type':'application/json','x-csrf-token':'a'*43}


@pytest.mark.parametrize('mutate',[
    lambda p:p.update(approved_by=str(uuid4())),lambda p:p.update(approved_at=1000),lambda p:p.update(tenant_id=str(uuid4())),
    lambda p:p.update(revision=True),lambda p:p.update(expires_at=1000),lambda p:p.update(expires_at=float('inf')),
    lambda p:p.update(rules=p['rules'][:-1]),lambda p:p['rules'][0].update(stores=['db']),
    lambda p:p['rules'][0].update(stores=p['rules'][0]['stores']+['db']),lambda p:p['rules'][0].update(regions=[[]]),
    lambda p:p['rules'][0].update(training_allowed=True),lambda p:p['rules'][0].update(retention_seconds=True),
])
def test_invalid_or_forged_policy_input_is_denied_before_repository(mutate):
    async def run():
        repo=SimpleNamespace(call=AsyncMock());service=PrivacyRegistryService(repo,HEADERS['origin'],clock=lambda:1000);p=payload();mutate(p)
        with pytest.raises(AccessFailure):await service.call('s'*43,'publish',p,HEADERS)
        repo.call.assert_not_awaited()
    asyncio.run(run())


def test_successful_policy_response_preserves_server_approval_and_original_csrf_binding():
    async def run():
        p=payload();record=dict(tenant_id=str(uuid4()),revision=1,expires_at=2000,rules=p['rules'],approved_by=str(uuid4()),approved_at=999)
        repo=SimpleNamespace(call=AsyncMock(return_value=dict(policy=record,revision=1,replayed=False,authority_expires_at=1100)))
        result=await PrivacyRegistryService(repo,HEADERS['origin'],clock=lambda:1000).call('s'*43,'publish',p,HEADERS)
        assert result['policy']==record and result['effective'] and result['_privacy_deadline']==1100
        assert set(repo.call.await_args.args[2])==set(p)|{'csrf_hash'} and 'approved_by' not in repo.call.await_args.args[2]
    asyncio.run(run())


def test_expired_latest_policy_is_visible_as_expired_configuration_not_effective_or_old_fallback():
    async def run():
        p=payload();record=dict(tenant_id=str(uuid4()),revision=2,expires_at=999,rules=p['rules'],approved_by=str(uuid4()),approved_at=900)
        repo=SimpleNamespace(call=AsyncMock(return_value=dict(policy=record,revision=2,authority_expires_at=1100)))
        result=await PrivacyRegistryService(repo,HEADERS['origin'],clock=lambda:1000).call('s'*43,'current',{})
        assert result['policy']['revision']==2 and result['effective'] is False
    asyncio.run(run())


@pytest.mark.parametrize('change',[dict(origin='https://wrong.test'),{'x-csrf-token':None},{'content-type':'text/plain'}])
def test_publication_csrf_origin_and_content_type_checked_before_database(change):
    async def run():
        repo=SimpleNamespace(call=AsyncMock())
        with pytest.raises(AccessFailure):await PrivacyRegistryService(repo,HEADERS['origin'],clock=lambda:1000).call('s'*43,'publish',payload(),dict(HEADERS,**change))
        repo.call.assert_not_awaited()
    asyncio.run(run())


def test_opt_in_flag_and_g13_targets_are_explicit_and_metadata_only():
    assert Settings(env='test',host='127.0.0.1',port=8000).privacy_enabled is False
    with pytest.raises(ValueError):Settings(env='test',host='127.0.0.1',port=8000,privacy_enabled=True)
    with pytest.raises(ValueError):Settings(env='test',host='127.0.0.1',port=8000,privacy_enabled='yes')
    assert read_target('/api/v1/operator/privacy-policy')==('privacy','current')
    assert mutation_target('POST','/api/v1/operator/privacy-policy')==('privacy','publish')
    from enterprise_ai_platform.infrastructure.database.migration import revisions,revision_sql
    current=next(r for r in revisions() if r.version==31)
    assert 'privacy_policy_call' in revision_sql(current,'db03_probe_'+'a'*32).as_string()


def test_privacy_response_deadline_checked_after_header_await():
    from enterprise_ai_platform.api.receipt_boundary import ReceiptResponseBoundary
    from enterprise_ai_platform.api.boundary import ResponseInterrupted
    now=[1];sent=[]
    async def app(scope,receive,send):
        await send(dict(type='http.response.start',status=200,headers=[]));await send(dict(type='http.response.body',body=b'private policy metadata'))
    async def send(message):
        sent.append(message)
        if message['type']=='http.response.start':now[0]=10
    async def receive():return dict(type='http.request',body=b'')
    scope=dict(type='http',path='/api/v1/operator/privacy-policy',state=dict(privacy_deadline=10,request_id='fixture'))
    with pytest.raises(RuntimeError):asyncio.run(ReceiptResponseBoundary(app,clock=lambda:now[0])(scope,receive,send))
    assert len(sent)==1 and sent[0]['type']=='http.response.start'


def test_inventory_keeps_owner_target_and_deadline_and_never_claims_erased():
    async def run():
        target=str(uuid4());repo=SimpleNamespace(call=AsyncMock(return_value=dict(subject_id=target,counts=dict(messages=2),execution_state='held',erased=False,authority_expires_at=1100)))
        result=await PrivacyRegistryService(repo,HEADERS['origin'],clock=lambda:1000).inventory('s'*43,target)
        assert result['subject_id']==target and result['erased'] is False and result['_privacy_deadline']==1100
        assert repo.call.await_args.args[1:]==('inventory',dict(id=target))
    asyncio.run(run())


@pytest.mark.parametrize('session,target',[('s'*43,'../other'),(None,str(uuid4()))])
def test_invalid_inventory_input_never_calls_store(session,target):
    async def run():
        repo=SimpleNamespace(call=AsyncMock())
        with pytest.raises(AccessFailure):await PrivacyRegistryService(repo,HEADERS['origin']).inventory(session,target)
        repo.call.assert_not_awaited()
    asyncio.run(run())
