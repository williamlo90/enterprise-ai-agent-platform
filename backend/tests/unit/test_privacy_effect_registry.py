import asyncio,copy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.privacy.domain import Category,REQUIRED
from enterprise_ai_platform.modules.privacy.effect_policy import CopyClass,ClockBasis,COPY_CLOCK,ALLOWED_CLASSES
from enterprise_ai_platform.modules.privacy.effect_codec import effect_policy_from_json
from enterprise_ai_platform.modules.privacy.registry import PrivacyRegistryService
from enterprise_ai_platform.modules.authorization.ports import AccessFailure
from enterprise_ai_platform.api.browser_context import read_target,mutation_target

HEADERS={'origin':'https://app.example.test','content-type':'application/json','x-csrf-token':'c'*43}


def payload():
    defaults={'db':CopyClass.CONTENT,'file':CopyClass.CONTENT,'index':CopyClass.INDEX,'cache':CopyClass.CACHE,'checkpoint':CopyClass.CHECKPOINT,'ledger':CopyClass.CANONICAL,'pins':CopyClass.CANONICAL,'backup':CopyClass.BACKUP,'provider':CopyClass.PROVIDER}
    return dict(key=str(uuid4()),schema_version=2,revision=1,expires_at=2000,copy_rules=[dict(category=c.value,store=s,copy_class=defaults[s].value,min_retain_seconds=0,expire_after_seconds=100,clock_basis=(ClockBasis.COPY_CREATED if defaults[s] in COPY_CLOCK else ClockBasis.CREATED).value,regions=['ap-southeast-1'],provider_retention_seconds=0 if s=='provider' else 'not_applicable',training_allowed=False) for c,stores in REQUIRED.items() for s in sorted(stores)])


@pytest.mark.parametrize('mutate',[
    lambda p:p.update(schema_version=1),lambda p:p.update(schema_version=True),lambda p:p.update(approved_by=str(uuid4())),
    lambda p:p.update(copy_rules=p['copy_rules'][:-1]),lambda p:p.update(copy_rules=p['copy_rules']+[p['copy_rules'][0]]),
    lambda p:p['copy_rules'][0].update(min_retain_seconds=101),lambda p:p['copy_rules'][0].update(expire_after_seconds=0),
    lambda p:p['copy_rules'][0].update(min_retain_seconds=None),lambda p:p['copy_rules'][0].update(clock_basis='created_at'),
    lambda p:p['copy_rules'][0].update(copy_class='content'),lambda p:p['copy_rules'][0].update(regions=[{}]),
    lambda p:p['copy_rules'][0].update(training_allowed=True),
])
def test_effect_policy_input_is_versioned_complete_and_closed_before_repository(mutate):
    async def run():
        p=payload();mutate(p);repo=SimpleNamespace(call=AsyncMock());service=PrivacyRegistryService(repo,HEADERS['origin'],clock=lambda:1000)
        with pytest.raises(AccessFailure):await service.effect_call('s'*43,'publish',p,HEADERS)
        repo.call.assert_not_awaited()
    asyncio.run(run())


def test_effect_policy_approval_comes_from_server_and_uses_separate_versioned_producer():
    async def run():
        p=payload();record=dict(schema_version=2,tenant_id=str(uuid4()),revision=1,approved_by=str(uuid4()),approved_at=999,expires_at=2000,copy_rules=p['copy_rules'])
        repo=SimpleNamespace(call=AsyncMock(return_value=dict(policy=record,authority_expires_at=1100)))
        result=await PrivacyRegistryService(repo,HEADERS['origin'],clock=lambda:1000).effect_call('s'*43,'publish',p,HEADERS)
        assert result['policy']==record and result['effective'] and repo.call.await_args.args[1]=='effect_publish'
        assert len(effect_policy_from_json(record).rules)==len(p['copy_rules'])
        assert 'approved_by' not in repo.call.await_args.args[2]
    asyncio.run(run())


def test_legacy_metadata_policy_cannot_be_decoded_as_effect_authority():
    p=payload();record=dict(tenant_id=str(uuid4()),revision=1,approved_by=str(uuid4()),approved_at=999,expires_at=2000,rules=[])
    with pytest.raises(Exception):effect_policy_from_json(record)
    assert read_target('/api/v1/operator/privacy-effect-policy')==('privacy_effect','current')
    assert mutation_target('POST','/api/v1/operator/privacy-effect-policy')==('privacy_effect','publish')
