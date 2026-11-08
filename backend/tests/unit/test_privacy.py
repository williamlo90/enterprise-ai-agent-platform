"""Local orchestration kernel; typed store rows are controlled fixtures."""
import asyncio
from dataclasses import replace
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.privacy.domain import Policy,Rule,Category,Grant,Asset,Inventory,PrivacyFailure,REQUIRED
from enterprise_ai_platform.modules.privacy.service import PrivacyService
from enterprise_ai_platform.infrastructure.database.privacy_local import LocalPrivacyRepository,LocalPrivacyJournal


def uid():return str(uuid4())


def fixture(tmp_path,*,operation='delete',category=Category.CONVERSATIONS):
    tenant,actor,subject=uid(),uid(),uid();namespace,owner,journal_id=uid(),uid(),uid()
    repo=LocalPrivacyRepository(tmp_path,namespace,owner);journal=LocalPrivacyJournal(tmp_path,journal_id,owner)
    policy=Policy(tenant,1,actor,100,2000,tuple(Rule(c,10,('ap-southeast-1',),REQUIRED[c],0) for c in Category))
    value=SimpleNamespace(policy=policy,grant=Grant(tenant,actor,subject,operation,category,1,2000),revoked=False,holds=0,hold_keys=set())
    class Authority:
        async def current(self,*_):
            if value.revoked:raise PrivacyFailure('denied')
            return value.grant
        async def hold(self,grant,operation_id,*,policy_digest):
            if value.revoked or grant!=value.grant or value.policy.fingerprint()!=policy_digest:raise PrivacyFailure('denied')
            if operation_id not in value.hold_keys:value.holds+=1;value.hold_keys.add(operation_id)
    class Policies:
        async def current(self,*_):return value.policy
    store=Stores(tmp_path/'fixture-stores.sqlite3',tenant,subject,category)
    def service():return PrivacyService(repo,Policies(),Authority(),store,journal,clock=lambda:1000)
    store.guard=lambda:(value.policy.fingerprint(),not value.revoked,value.grant)
    value.service=service;value.repo=repo;value.journal=journal;value.store=store
    value.tenant,value.actor,value.subject=tenant,actor,subject;value.operation=operation;value.category=category;value.opid=uid()
    value.restart=lambda:LocalPrivacyRepository(tmp_path,namespace,owner)
    return value


class Stores:
    def __init__(self,path,tenant,subject,category):
        self.path,self.tenant,self.subject,self.category=path,tenant,subject,category
        self.assets=tuple(Asset(s,uid(),'1',hashlib.sha256(s.encode()).hexdigest(),'ap-southeast-1',0,0) for s in sorted(REQUIRED[category]))
        self.coverage=REQUIRED[category];self.calls=0;self.changed=False;self.crash=False;self.after_apply=None
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS objects(tenant TEXT,subject TEXT,store TEXT,id TEXT,version TEXT,PRIMARY KEY(tenant,id))')
            db.execute('CREATE TABLE IF NOT EXISTS effects(key TEXT PRIMARY KEY,operation TEXT)')
            db.executemany('INSERT OR IGNORE INTO objects VALUES(?,?,?,?,?)',[(tenant,subject,a.store,a.resource_id,a.version) for a in self.assets])
    async def inventory(self,grant,category):return Inventory(self.tenant,self.subject,category,self.coverage,self.assets)
    async def current(self,grant,asset,*,policy_digest):return not self.changed and self.guard()==(policy_digest,True,grant)
    async def reconcile(self,grant,asset,operation,key,*,policy_digest):
        with sqlite3.connect(self.path) as db:row=db.execute('SELECT operation FROM effects WHERE key=?',(key,)).fetchone()
        if row and row[0]=='sealed:'+operation:return 'sealed_not_applied'
        return 'applied' if row and row[0]==operation else 'unknown'
    async def apply(self,grant,asset,operation,key,*,policy_digest):
        if self.guard()!=(policy_digest,True,grant):raise PrivacyFailure('denied')
        self.calls+=1
        with sqlite3.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            assert grant.tenant_id==self.tenant and grant.subject_id==self.subject
            if db.execute('SELECT 1 FROM effects WHERE key=?',(key,)).fetchone():raise PrivacyFailure('denied')
            if operation=='delete':db.execute('DELETE FROM objects WHERE tenant=? AND subject=? AND id=? AND version=?',(grant.tenant_id,grant.subject_id,asset.resource_id,asset.version))
            db.execute('INSERT INTO effects VALUES(?,?)',(key,operation))
        if self.after_apply:self.after_apply()
        if self.crash:raise RuntimeError('simulated lost acknowledgment after durable effect')
        return 'applied'


async def start(v):return await v.service().start('fixture',v.tenant,v.subject,v.operation,v.category,v.opid)
async def advance(v):return await v.service().advance('fixture',v.tenant,v.opid)


@pytest.mark.parametrize('change',[
    lambda p:replace(p,rules=p.rules[:-1]),
    lambda p:replace(p,rules=(p.rules[0],)*8),
    lambda p:replace(p,approved_at=1100),
    lambda p:replace(p,expires_at=1000),
    lambda p:replace(p,rules=(replace(p.rules[0],retention_seconds=True),)+p.rules[1:]),
    lambda p:replace(p,rules=(replace(p.rules[0],training_allowed=True),)+p.rules[1:]),
    lambda p:replace(p,rules=(replace(p.rules[0],stores=frozenset({'db'})),)+p.rules[1:]),
])
def test_incomplete_or_unapproved_policy_blocks_before_scope_hold_and_plan(tmp_path,change):
    async def run():
        v=fixture(tmp_path);v.policy=change(v.policy)
        with pytest.raises(PrivacyFailure):await start(v)
        assert v.holds==0 and await v.repo.get(v.tenant,v.opid) is None and v.store.calls==0
    asyncio.run(run())


@pytest.mark.parametrize('operation',['delete','export'])
def test_actual_owned_metadata_restart_idempotency_and_all_store_steps(tmp_path,operation):
    async def run():
        v=fixture(tmp_path,operation=operation);body=await start(v)
        assert body==await start(v) and v.holds==(1 if operation=='delete' else 0)
        v.repo=v.restart()
        for _ in range(len(body['steps'])+1):body=await advance(v)
        assert body['state']=='complete' and v.store.calls==len(v.store.assets)
        assert await advance(v)==body and v.store.calls==len(v.store.assets)
        with sqlite3.connect(v.store.path) as db:
            assert db.execute('SELECT count(*) FROM objects').fetchone()[0]==(0 if operation=='delete' else len(v.store.assets))
        with v.journal.connect() as db:
            rows=db.execute('SELECT kind,count(*) FROM events GROUP BY kind').fetchall()
            assert dict(rows)==dict(intent=len(v.store.assets)+1,result=len(v.store.assets)+1)
    asyncio.run(run())


def test_receipt_pins_or_unknown_ledger_hold_the_entire_delete_before_any_effect(tmp_path):
    async def run():
        v=fixture(tmp_path,category=Category.RECEIPTS);v.store.assets=tuple(replace(a,unresolved=a.store=='pins') for a in v.store.assets)
        body=await start(v);assert body['state']=='held' and body['reason']=='retention_or_unresolved'
        assert (await advance(v))['state']=='held' and v.store.calls==0
    asyncio.run(run())


def test_policy_retention_clock_and_residency_are_enforced_not_only_asset_flags(tmp_path):
    async def run():
        v=fixture(tmp_path);v.store.assets=(replace(v.store.assets[0],created_at=999),)
        assert (await start(v))['reason']=='retention_or_unresolved'
        v=fixture(tmp_path/'other');v.store.assets=(replace(v.store.assets[0],region='unapproved'),)
        assert (await start(v))['reason']=='residency' and v.store.calls==0
    asyncio.run(run())


def test_missing_derived_store_coverage_or_cross_tenant_inventory_is_never_complete(tmp_path):
    async def run():
        v=fixture(tmp_path);v.store.coverage=frozenset({'db'})
        with pytest.raises(PrivacyFailure):await start(v)
        assert v.store.calls==0
        v.store.coverage=REQUIRED[v.category];v.store.tenant=uid()
        with pytest.raises(PrivacyFailure):await start(v)
        assert await v.repo.get(v.tenant,v.opid) is None
    asyncio.run(run())


def test_lost_ack_reconciles_durable_effect_without_repeating_it(tmp_path):
    async def run():
        v=fixture(tmp_path);await start(v);v.store.crash=True
        with pytest.raises(RuntimeError):await advance(v)
        assert v.store.calls==1
        v.repo=v.restart();v.store.crash=False
        assert (await advance(v))['steps'][0]['state']=='complete' and v.store.calls==1
    asyncio.run(run())


def test_unknown_effect_keeps_intent_without_rearming_clobbering_or_reporting_deleted(tmp_path):
    async def run():
        v=fixture(tmp_path);body=await start(v);body['steps'][0]['state']='intent';await v.repo.save(v.tenant,v.opid,1,body)
        body=await advance(v)
        assert body['state']=='queued' and body['steps'][0]['state']=='intent' and v.store.calls==0
        assert (await v.repo.get(v.tenant,v.opid))['revision']==2
    asyncio.run(run())


def test_policy_changed_after_intent_stops_before_effect(tmp_path):
    async def run():
        v=fixture(tmp_path);await start(v);original=v.journal.intent
        async def changed(*args):
            await original(*args);v.policy=replace(v.policy,revision=2)
        v.journal.intent=changed
        body=await advance(v)
        assert body['state']=='held' and body['reason']=='pre_effect_changed' and v.store.calls==0
    asyncio.run(run())


def test_revocation_after_known_effect_keeps_result_but_holds_following_steps(tmp_path):
    async def run():
        v=fixture(tmp_path);await start(v);v.store.after_apply=lambda:setattr(v,'revoked',True)
        with pytest.raises(PrivacyFailure):await advance(v)
        body=await v.repo.get(v.tenant,v.opid)
        assert body['state']=='held' and body['steps'][0]['state']=='complete' and v.store.calls==1
        with v.journal.connect() as db:assert db.execute("SELECT count(*) FROM events WHERE body=?",('{"outcome":"applied"}',)).fetchone()[0]==1
        with pytest.raises(PrivacyFailure):await advance(v)
    asyncio.run(run())


def test_journal_intent_outage_never_executes_effect(tmp_path):
    async def run():
        v=fixture(tmp_path);await start(v)
        async def unavailable(*_):raise PrivacyFailure('unavailable')
        v.journal.intent=unavailable
        with pytest.raises(PrivacyFailure):await advance(v)
        assert v.store.calls==0 and (await v.repo.get(v.tenant,v.opid))['steps'][0]['state']=='pending'
    asyncio.run(run())


def test_request_key_conflict_stale_cas_and_foreign_database_adoption_denied(tmp_path):
    async def run():
        v=fixture(tmp_path);body=await start(v)
        with pytest.raises(PrivacyFailure):await v.service().start('fixture',v.tenant,uid(),'delete',v.category,v.opid)
        saved=await v.repo.save(v.tenant,v.opid,1,body)
        with pytest.raises(PrivacyFailure):await v.repo.save(v.tenant,v.opid,1,body)
        with pytest.raises(PrivacyFailure):LocalPrivacyRepository(tmp_path,v.repo.path.stem.removeprefix('privacy-'),uid())
        assert (await v.repo.get(v.tenant,v.opid))==saved
        assert await v.repo.get(uid(),v.opid) is None
    asyncio.run(run())


def test_concurrent_step_cas_admits_one_effect(tmp_path):
    async def run():
        v=fixture(tmp_path);await start(v);count=0;gate=asyncio.Event();original=v.store.current
        async def concurrent(*args,**kwargs):
            nonlocal count
            count+=1
            if count<=2:
                if count==2:gate.set()
                await gate.wait()
            return await original(*args,**kwargs)
        v.store.current=concurrent
        results=await asyncio.gather(advance(v),advance(v),return_exceptions=True)
        assert v.store.calls==1 and sum(isinstance(r,PrivacyFailure) for r in results)==1
    asyncio.run(run())


def test_lost_journal_result_after_effect_recovers_without_reexecution(tmp_path):
    async def run():
        v=fixture(tmp_path);await start(v);original=v.journal.result
        async def outage(*args):
            if args[-1]=='applied':raise PrivacyFailure('unavailable')
            await original(*args)
        v.journal.result=outage
        with pytest.raises(PrivacyFailure):await advance(v)
        assert (await v.repo.get(v.tenant,v.opid))['steps'][0]['state']=='intent' and v.store.calls==1
        v.journal.result=original
        assert (await advance(v))['steps'][0]['state']=='complete' and v.store.calls==1
    asyncio.run(run())


def test_revocation_during_plan_commit_does_not_return_metadata_to_revoked_actor(tmp_path):
    async def run():
        v=fixture(tmp_path);original=v.repo.create
        async def revoked(*args):
            value=await original(*args);v.revoked=True;return value
        v.repo.create=revoked
        with pytest.raises(PrivacyFailure):await start(v)
        assert await v.repo.get(v.tenant,v.opid) is not None and v.store.calls==0
    asyncio.run(run())


def test_journal_rejects_result_without_intent_conflicting_result_and_mutation(tmp_path):
    async def run():
        v=fixture(tmp_path);key='a'*64
        with pytest.raises(PrivacyFailure):await v.journal.result(v.tenant,v.opid,key,'applied')
        await v.journal.intent(v.tenant,v.opid,key,dict(opaque_ref=uid()))
        await v.journal.result(v.tenant,v.opid,key,'unknown')
        with pytest.raises(PrivacyFailure):await v.journal.result(v.tenant,v.opid,key,'applied')
        with pytest.raises(sqlite3.IntegrityError):
            with v.journal.connect() as db:db.execute('DELETE FROM events')
    asyncio.run(run())


@pytest.mark.parametrize('change',[
    lambda g:replace(g,category=Category.AUDIT),
    lambda g:replace(g,tenant_id=uid()),
    lambda g:replace(g,subject_id=uid()),
    lambda g:replace(g,expires_at=1000),
])
def test_authority_is_exact_tenant_subject_category_and_unexpired(tmp_path,change):
    async def run():
        v=fixture(tmp_path);v.grant=change(v.grant)
        with pytest.raises(PrivacyFailure):await start(v)
        assert v.holds==0 and v.store.calls==0
    asyncio.run(run())


def test_owning_effect_fixture_rechecks_revocation_after_last_external_precheck(tmp_path):
    async def run():
        v=fixture(tmp_path);await start(v);original=v.store.current;count=0
        async def raced(*args,**kwargs):
            nonlocal count
            count+=1;result=await original(*args,**kwargs)
            if count==2:v.revoked=True
            return result
        v.store.current=raced
        with pytest.raises(PrivacyFailure):await advance(v)
        assert v.store.calls==0 and (await v.repo.get(v.tenant,v.opid))['steps'][0]['state']=='intent'
    asyncio.run(run())


def test_second_worker_observes_live_intent_without_clobbering_first_worker(tmp_path):
    async def run():
        v=fixture(tmp_path);await start(v);entered=asyncio.Event();release=asyncio.Event();original=v.store.apply
        async def paused(*args,**kwargs):
            entered.set();await release.wait();return await original(*args,**kwargs)
        v.store.apply=paused
        first=asyncio.create_task(advance(v));await entered.wait()
        before=await v.repo.get(v.tenant,v.opid);second=await advance(v)
        assert second==before and second['steps'][0]['state']=='intent' and v.store.calls==0
        release.set();completed=await first
        assert completed['state']=='queued' and completed['steps'][0]['state']=='complete' and v.store.calls==1
        with v.journal.connect() as db:assert db.execute("SELECT count(*) FROM events WHERE body=?",('{"outcome":"applied"}',)).fetchone()[0]==1
    asyncio.run(run())


def test_durable_not_applied_seal_rejects_late_writer_and_never_rearms(tmp_path):
    async def run():
        v=fixture(tmp_path);await start(v);entered=asyncio.Event();release=asyncio.Event();original=v.store.apply
        async def paused(*args,**kwargs):
            entered.set();await release.wait();return await original(*args,**kwargs)
        v.store.apply=paused;first=asyncio.create_task(advance(v));await entered.wait()
        body=await v.repo.get(v.tenant,v.opid);step=body['steps'][0]
        with sqlite3.connect(v.store.path) as db:db.execute('INSERT INTO effects VALUES(?,?)',(step['effect_key'],'sealed:delete'))
        result=await advance(v)
        assert result['state']=='held' and result['reason']=='effect_sealed_manual_review'
        release.set()
        with pytest.raises(PrivacyFailure):await first
        with sqlite3.connect(v.store.path) as db:assert db.execute('SELECT count(*) FROM objects').fetchone()[0]==len(v.store.assets)
        assert (await advance(v))['steps'][0]['state']=='intent'
    asyncio.run(run())


@pytest.mark.parametrize('change',['revoked','policy_changed'])
def test_revocation_during_scope_intent_write_prevents_hold_side_effect(tmp_path,change):
    async def run():
        v=fixture(tmp_path);original=v.journal.intent
        async def revoked(*args):
            await original(*args)
            if change=='revoked':v.revoked=True
            else:v.policy=replace(v.policy,revision=2)
        v.journal.intent=revoked
        with pytest.raises(PrivacyFailure):await start(v)
        assert v.holds==0 and await v.repo.get(v.tenant,v.opid) is None
    asyncio.run(run())
