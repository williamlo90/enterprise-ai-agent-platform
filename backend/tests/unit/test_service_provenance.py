import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock
import time
import pytest
from enterprise_ai_platform.integrations.reimbursement.provenance import ServiceProvenance,ServicePermit
from enterprise_ai_platform.infrastructure.database.service_sources import staged,ServiceStage,ServiceGate
from enterprise_ai_platform.modules.conversations.domain import SourceRef,public_messages,ConversationFailure
from enterprise_ai_platform.modules.receipts.domain import ClaimAccess,ReceiptRef

def fixture():
    tenant,actor,claim,file=[str(uuid4()) for _ in range(4)]
    context=dict(tenant_id=tenant,actor_id=actor,membership_id=str(uuid4()),session_id=str(uuid4()),access_revision=1,authority_expires_at=time.time()+60)
    ref=SourceRef('service','claim',claim,'1','active_reviewer','1');receipt=SourceRef('service','receipt',file,'3','active_reviewer','1')
    data=dict(id=claim,version=1,revision=1,owner_id=str(uuid4()),reviewer_id=actor,state='pending_approval',active_stage=1,receipts=[dict(id=file,version=3,digest='a'*64)])
    access=ClaimAccess(tenant,actor,claim,1,1,(ReceiptRef(file,3,'a'*64),),int(time.time()+50))
    reader=SimpleNamespace(read=AsyncMock(return_value=({'claim':data},time.time()+50)))
    authority=SimpleNamespace(authorize=AsyncMock(return_value=(access,{'proof':'fictional'})))
    return context,ref,receipt,data,access,reader,authority

def test_live_verifier_claim_and_companion_receipt_require_current_exact_relation_and_versions():
    c,r,f,d,a,reader,authority=fixture();v=ServiceProvenance(reader,authority)
    p=asyncio.run(v.verify(c,(r,f)));assert p.allowed=={r:True,f:True};assert p.envelopes
    assert reader.read.call_args.args[1]==dict(view='approvals',claim_id=r.resource_id,after=None,limit=1)
    assert asyncio.run(v.verify(c,(f,))).allowed[f] is False
    for bad in [replace(r,version='2'),replace(r,observed_revision='01'),replace(r,relation='owner'),replace(f,version='2')]:
        p=asyncio.run(v.verify(c,(r,bad)));assert not p.allowed[bad]
    assert not asyncio.run(v.verify(c,(r,replace(r,resource_id=str(uuid4())),f))).allowed[f]

@pytest.mark.parametrize('change',[{'reviewer_id':str(uuid4())},{'owner_id':'self'},{'active_stage':None},{'state':'approved'},{'version':2},{'revision':2}])
def test_changed_source_relationship_denies_derived_access(change):
    c,r,f,d,a,reader,authority=fixture();d.update({k:c['actor_id'] if v=='self' else v for k,v in change.items()});p=asyncio.run(ServiceProvenance(reader,authority).verify(c,(r,f)));assert not any(p.allowed.values())

@pytest.mark.parametrize('which',['outage','malformed','expired','wrong_actor'])
def test_outage_malformed_expired_and_cross_actor_attestation_fail_closed(which):
    c,r,f,d,a,reader,authority=fixture()
    if which=='outage':reader.read.side_effect=TimeoutError
    if which=='malformed':reader.read.return_value=({},time.time()+50)
    if which=='expired':authority.authorize.return_value=(replace(a,expires_at=int(time.time()-1)),{})
    if which=='wrong_actor':authority.authorize.return_value=(replace(a,actor_id=str(uuid4())),{})
    p=asyncio.run(ServiceProvenance(reader,authority).verify(c,(r,f)));assert not any(p.allowed.values())

@pytest.mark.parametrize('expiry',[float('nan'),float('inf'),True])
def test_invalid_permit_never_authorizes(expiry):
    with pytest.raises(ValueError):ServicePermit({}, {}, (), expiry)

def test_unsupported_trip_provenance_and_missing_fingerprint_are_not_grants():
    c,r,f,d,a,reader,authority=fixture();trip=SourceRef('service','trip',str(uuid4()),'1','owner','1');assert not asyncio.run(ServiceProvenance(reader,authority).verify(c,(trip,))).allowed[trip]
    with pytest.raises(ValueError):ServicePermit({r:True},{},(),time.time()+50)

def test_staged_reads_release_owner_uow_then_recheck_after_buffer_and_redact_whole_message():
    c,r,f,d,a,reader,authority=fixture();snap=ServiceStage(c,(r,),'snapshot');events=[];calls=0
    good=ServicePermit({r:True},{r:'stable'},(),time.time()+30);bad=ServicePermit({r:False},{},(),time.time()+30)
    verifier=SimpleNamespace(verify=AsyncMock(side_effect=[good,good,bad]))
    message=dict(id=str(uuid4()),sequence=1,author_kind='model',run_id=None,output_kind='answer',created_at='fixture',body='sensitive',provenance_mode='dependent',_sources=[r.__dict__],feedback={'reason':'private'},citations=[{'id':'private'}])
    async def local(gate):
        nonlocal calls
        calls+=1;events.extend(['begin','commit'])
        if gate is None:raise snap
        permit={r:gate.allowed[r]};return dict(messages=public_messages([message],permit),authority_expires_at=time.time()+40)
    async def verify(*args):
        assert events[-1]=='commit';return [good,good,bad][verifier.verify.await_count-1]
    verifier.verify.side_effect=verify
    result=asyncio.run(staged(local,verifier,redact=True));assert calls==4
    m=result['messages'][0];assert m['redacted'] and m['text'] is None and m['sources']==[] and m['citations']==[] and m['feedback'] is None

def test_owner_or_reference_drift_blocks_before_final_read():
    c,r,f,d,a,reader,authority=fixture();n=0
    async def local(gate):
        nonlocal n
        n+=1;raise ServiceStage(c,(r,),'first' if n==1 else 'changed')
    verifier=SimpleNamespace(verify=AsyncMock(return_value=ServicePermit({r:True},{r:'same'},(),time.time()+30)))
    with pytest.raises(ConversationFailure) as e:asyncio.run(staged(local,verifier))
    assert e.value.status==409 and verifier.verify.await_count==1

def test_source_snapshot_exception_repr_does_not_dump_private_context():
    c,r,*_=fixture();e=ServiceStage(c,(r,),'private');assert c['session_id'] not in str(e) and c['actor_id'] not in repr(e)


def test_final_remote_await_requires_fresh_local_document_and_receipt_recheck():
    c,r,*_=fixture();snap=ServiceStage(c,(r,),'snapshot');count=0;local_allowed=True;local_commits=[]
    permit=ServicePermit({r:True},{r:'same'},(),time.time()+30)
    async def verify(*args):
        nonlocal count,local_allowed
        count+=1
        if count==3:local_allowed=False
        return permit
    async def local(gate):
        if gate is None:raise snap
        local_commits.append(local_allowed)
        return dict(visible=local_allowed,authority_expires_at=time.time()+40)
    result=asyncio.run(staged(local,SimpleNamespace(verify=verify)))
    assert local_commits==[True,False] and result['visible'] is False
