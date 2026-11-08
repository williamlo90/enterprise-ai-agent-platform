import asyncio
from dataclasses import replace
import time
from uuid import uuid4
import pytest
from reimbursement_service.modules.operations.domain import Identity,Result,Terminal,OperationFailure
from reimbursement_service.modules.operations.service import OperationsService
from reimbursement_service.modules.access.domain import VerifiedActor,request_hash


def payload():
    return dict(schema_version=1,action='claim.decide',tenant_id=str(uuid4()),actor_id=str(uuid4()),
        operation_id=str(uuid4()),object=dict(kind='claim',id=str(uuid4()),version=1),
        data=dict(revision=1,stage=1,decision='approve',reason=''))


def actor(p):
    now=int(time.time())
    return VerifiedActor(p['actor_id'],p['tenant_id'],str(uuid4()),1,p['action'],p['operation_id'],request_hash(p),now,now+60,False)


class Repo:
    def __init__(self):self.value=None;self.effects=0
    async def commit(self,identity,actor,work):
        if self.value is None:
            self.effects+=1
            self.value=Terminal(identity,'committed',Result(str(uuid4()),1,'fixture'),int(time.time()))
        return self.value
    async def lookup(self,operation_id,actor):return self.value
    async def existing(self,identity,actor):
        if self.value is not None and self.value.identity!=identity:raise OperationFailure(409)
        return self.value


class FailingSigner:
    def issue(self,terminal,*,authority_deadline):raise OperationFailure(503)


class Proofs:
    def issue(self,terminal,*,authority_deadline):return {'fixture':'proof-port'}


def test_post_commit_signing_failure_never_replays_effect():
    async def run():
        p=payload();person=actor(p);repository=Repo();key=str(uuid4())
        service=OperationsService(repository,FailingSigner())
        with pytest.raises(OperationFailure):await service.commit(p,key,person,None)
        assert repository.effects==1 and repository.value.state=='committed'
        service.proofs=Proofs()
        response=await service.commit(p,key,person,None)
        assert repository.effects==1 and response['operation']==repository.value.wire()
    asyncio.run(run())


@pytest.mark.parametrize('change',[dict(scope='owner'),dict(expires_at=0),dict(actor_id=str(uuid4())),dict(request_hash='0'*64)])
def test_unbound_or_expired_actor_prevents_work(change):
    async def run():
        p=payload();repository=Repo();service=OperationsService(repository,Proofs())
        with pytest.raises(OperationFailure):await service.commit(p,str(uuid4()),replace(actor(p),**change),None)
        assert repository.effects==0
    asyncio.run(run())


def test_identity_and_result_shape_reject_bool_and_foreign_fields():
    p=payload()
    with pytest.raises(ValueError):Identity(p['tenant_id'],p['operation_id'],p['actor_id'],'platform-dispatcher','claim.decide',str(uuid4()),request_hash(p),'claim',p['object']['id'],True)
    for values in [(None,1,'fixture'),(str(uuid4()),None,'fixture'),(None,None,'secret full content')]:
        with pytest.raises(ValueError):Result(*values)


def test_exact_mutation_replay_is_read_only_and_returns_committed_terminal():
    async def run():
        p=payload();person=actor(p);repo=Repo();key=str(uuid4());service=OperationsService(repo,Proofs())
        assert await service.replay(p,key,person) is None and repo.effects==0
        committed=await service.commit(p,key,person,None)
        replay=await service.replay(p,key,person)
        assert replay==committed and repo.effects==1
        with pytest.raises(OperationFailure) as error:await service.replay(p,str(uuid4()),person)
        assert error.value.status==409 and repo.effects==1
    asyncio.run(run())


@pytest.mark.parametrize('change',[dict(scope='catalog'),dict(expires_at=0),dict(operation_id=str(uuid4())),dict(request_hash='0'*64)])
def test_replay_preflight_requires_exact_current_original_actor_binding(change):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    async def run():
        p=payload();repo=SimpleNamespace(existing=AsyncMock())
        service=OperationsService(repo,Proofs())
        with pytest.raises(OperationFailure):await service.replay(p,str(uuid4()),replace(actor(p),**change))
        repo.existing.assert_not_called()
    asyncio.run(run())


def test_replay_does_not_sign_an_unrelated_terminal_returned_by_repository():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    async def run():
        p=payload();person=actor(p);key=str(uuid4())
        wrong=Identity(p['tenant_id'],str(uuid4()),p['actor_id'],'platform-dispatcher',p['action'],key,request_hash(p),'claim',p['object']['id'],1)
        terminal=Terminal(wrong,'committed',Result(str(uuid4()),1,'fixture'),int(time.time()))
        service=OperationsService(SimpleNamespace(existing=AsyncMock(return_value=terminal)),Proofs())
        with pytest.raises(OperationFailure) as error:await service.replay(p,key,person)
        assert error.value.status==503
    asyncio.run(run())
