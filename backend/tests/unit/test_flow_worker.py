import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.jobs.ports import Lease
from enterprise_ai_platform.modules.flows.service import FlowWorker
from enterprise_ai_platform.modules.flows.domain import VerifiedTerminal,FlowFailure


class Repository:
    def __init__(self,mode='dispatch'):self.mode=mode;self.calls=[];self.payload=dict(mode=mode,identity={'operation_id':'fixture'},expires_at=200)
    async def call(self,operation,values):
        self.calls.append((operation,values))
        if operation=='prepare':return dict(self.payload)
        if operation=='pins':return dict(batch=None)
        return dict(state='committed' if operation=='settle' else 'unknown')


class Channel:
    def __init__(self):self.calls=[];self.result={'state':'committed'};self.lookup_result=None;self.error=False
    async def execute(self,packet):
        self.calls.append('execute')
        if self.error:raise TimeoutError()
        return self.result
    async def lookup(self,packet):self.calls.append('lookup');return self.lookup_result
    async def seal(self,packet):self.calls.append('seal');return {'state':'sealed_no_commit'}


class Proofs:
    def __init__(self):self.bad=False
    def verify(self,response,identity):
        if self.bad:raise FlowFailure('invalid')
        return VerifiedTerminal(json.dumps(response).encode(),200)
    def settlement(self,terminal,identity,batch):return {},None


class Jobs:
    def __init__(self):self.beats=0
    async def heartbeat(self,lease):self.beats+=1


def setup(mode='dispatch'):
    repo=Repository(mode);channel=Channel();proofs=Proofs();jobs=Jobs()
    return repo,channel,proofs,jobs,FlowWorker(repo,channel,proofs,jobs,'a'*64),Lease(uuid4(),uuid4(),1)


def test_dispatch_sends_once_and_settles_only_after_independent_proof():
    repo,channel,_,_,worker,lease=setup();result=asyncio.run(worker.process(lease))
    assert result['state']=='committed' and channel.calls==['execute']
    assert [op for op,_ in repo.calls]==['prepare','pins','settle']


def test_lost_response_records_recovery_without_resending_mutation():
    repo,channel,_,_,worker,lease=setup();channel.error=True
    assert asyncio.run(worker.process(lease))['state']=='unknown'
    assert channel.calls==['execute'] and [op for op,_ in repo.calls]==['prepare','uncertain']


def test_restart_uses_lookup_then_atomic_seal_for_404_instead_of_blind_retry():
    repo,channel,_,jobs,worker,lease=setup('recover');asyncio.run(worker.process(lease))
    assert channel.calls==['lookup','seal'] and jobs.beats==2
    assert [op for op,_ in repo.calls]==['prepare','prepare','pins','settle']


def test_found_commit_is_settled_without_seal_or_reexecuting_business_work():
    repo,channel,_,_,worker,lease=setup('recover');channel.lookup_result={'state':'committed'}
    asyncio.run(worker.process(lease));assert channel.calls==['lookup']


def test_known_immutable_rejection_is_held_without_seal_loop_or_pin_release():
    repo,channel,_,_,worker,lease=setup('recover');channel.lookup_result={'state':'rejected'}
    assert asyncio.run(worker.process(lease))['state']=='unknown'
    assert channel.calls==['lookup'] and [op for op,_ in repo.calls]==['prepare','hold']


def test_forged_or_expired_proof_never_reaches_settlement():
    repo,_,proofs,_,worker,lease=setup();proofs.bad=True
    asyncio.run(worker.process(lease));assert [op for op,_ in repo.calls]==['prepare','uncertain']
