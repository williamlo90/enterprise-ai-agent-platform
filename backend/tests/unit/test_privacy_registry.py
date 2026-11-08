import asyncio
from dataclasses import replace
import sqlite3
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.privacy.domain import Policy,PolicyDraft,Rule,Category,REQUIRED,PrivacyFailure
from enterprise_ai_platform.modules.privacy.policies import PrivacyPolicyService
from enterprise_ai_platform.modules.privacy.ports import DenyPolicyPublisher
from enterprise_ai_platform.infrastructure.database.privacy_local import LocalPrivacyPolicyRegistry


def uid():return str(uuid4())
def policy(tenant=None,revision=1):
    return Policy(tenant or uid(),revision,uid(),100,2000,tuple(Rule(c,10,('ap-southeast-1',),REQUIRED[c],0) for c in Category))
def draft(p):return PolicyDraft(p.tenant_id,p.revision,p.expires_at,p.rules)


def test_registry_empty_current_restart_idempotence_and_immutable_snapshots(tmp_path):
    async def run():
        namespace,owner=uid(),uid();repo=LocalPrivacyPolicyRegistry(tmp_path,namespace,owner);p=policy()
        assert await repo.current(p.tenant_id) is None
        await repo.append(p);assert await repo.append(p)==p
        repo=LocalPrivacyPolicyRegistry(tmp_path,namespace,owner)
        assert await repo.current(p.tenant_id)==p and await repo.current(uid()) is None
        with pytest.raises(sqlite3.IntegrityError):
            with repo.connect() as db:db.execute('UPDATE policies SET revision=2')
        with pytest.raises(sqlite3.IntegrityError):
            with repo.connect() as db:db.execute('DELETE FROM policies')
    asyncio.run(run())


def test_same_revision_conflict_gaps_and_latest_expired_never_fall_back(tmp_path):
    async def run():
        repo=LocalPrivacyPolicyRegistry(tmp_path,uid(),uid());p=policy();await repo.append(p)
        with pytest.raises(PrivacyFailure):await repo.append(replace(p,approved_by=uid()))
        with pytest.raises(PrivacyFailure):await repo.append(replace(p,revision=3))
        expired=replace(p,revision=2,expires_at=500);await repo.append(expired)
        current=await repo.current(p.tenant_id);assert current==expired
        with pytest.raises(PrivacyFailure):current.validate(1000)
    asyncio.run(run())


def test_approval_is_explicit_exact_committed_producer_output_not_input_boolean(tmp_path):
    async def run():
        repo=LocalPrivacyPolicyRegistry(tmp_path,uid(),uid());p=policy();denied=PrivacyPolicyService(repo,DenyPolicyPublisher(),clock=lambda:1000)
        with pytest.raises(PrivacyFailure):await denied.publish('fixture',draft(p))
        assert await repo.current(p.tenant_id) is None
        class ApprovedFixture:
            async def approve(self,credential,candidate):
                assert candidate==draft(p) and credential=='fixture'
                return p
        service=PrivacyPolicyService(repo,ApprovedFixture(),clock=lambda:1000)
        assert await service.publish('fixture',draft(p))==p
        with pytest.raises(PrivacyFailure):await service.publish('fixture',p)
    asyncio.run(run())


@pytest.mark.parametrize('change',[
    lambda p:replace(p,tenant_id=uid()),
    lambda p:replace(p,revision=2),
    lambda p:replace(p,expires_at=1500),
    lambda p:replace(p,rules=(replace(p.rules[0],retention_seconds=50),)+p.rules[1:]),
])
def test_publisher_cannot_substitute_approved_tenant_version_retention_or_expiry(tmp_path,change):
    async def run():
        repo=LocalPrivacyPolicyRegistry(tmp_path,uid(),uid());p=policy()
        class WrongFixture:
            async def approve(self,*_):return change(p)
        with pytest.raises(PrivacyFailure):await PrivacyPolicyService(repo,WrongFixture(),clock=lambda:1000).publish('fixture',draft(p))
        assert await repo.current(p.tenant_id) is None
    asyncio.run(run())


def test_identity_session_and_provider_category_cannot_be_omitted(tmp_path):
    p=policy();p.validate(1000)
    assert REQUIRED[Category.IDENTITY]==frozenset({'db','cache','ledger','backup','provider'})
    missing=replace(p,rules=tuple(r for r in p.rules if r.category is not Category.IDENTITY))
    with pytest.raises(PrivacyFailure):missing.validate(1000)
    with pytest.raises(PrivacyFailure):draft(missing).validate(1000)
