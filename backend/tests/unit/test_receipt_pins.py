from dataclasses import replace
from hashlib import sha256
import base64
import copy
import json
from uuid import UUID
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from enterprise_ai_platform.modules.receipts.domain import (
    ReceiptFailure,ReceiptRef,PinIdentity,PinBatch,PinState,TerminalEvidence,
    ClaimAccess,receipt_refs,finalise,encoded,
)
from enterprise_ai_platform.integrations.security.receipt_terminal import ReceiptTerminalVerifier,TerminalKey


def uid(n):return str(UUID(int=n))


def batch():
    return PinBatch(PinIdentity(uid(1),uid(2),uid(3),'platform-dispatcher','claim.submit',uid(4),'a'*64,'draft',uid(5),1),uid(6),
        (ReceiptRef(uid(7),2,'b'*64),))


def evidence(b=None,*,state='committed'):
    b=b or batch()
    return TerminalEvidence(b.identity,state,b.claim_id if state=='committed' else None,1 if state=='committed' else None,
        'claim_submitted' if state=='committed' else 'sealed_no_commit',100,'c'*64,150)


def proof_fixture(*,state='committed'):
    b=batch();private=Ed25519PrivateKey.generate()
    result={'ref':b.claim_id,'version':1,'code':'claim_submitted'} if state=='committed' else {'ref':None,'version':None,'code':'sealed_no_commit'}
    terminal=dict(identity=b.identity.wire(),state=state,result=result,result_digest=sha256(b'reimbursement:operation-result:v1:'+encoded(result)).hexdigest(),completed_at=99,ledger_revision=1)
    body=dict(schema_version=1,issuer='reference-reimbursement',audience='enterprise-ai-platform',kid='test',operation=terminal,issued_at=100,expires_at=150)
    trust=TerminalKey(private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),90,200)
    return b,private,body,trust


def sign(private,body):
    return dict(payload=copy.deepcopy(body),signature=base64.urlsafe_b64encode(private.sign(b'reimbursement:terminal-proof:v1:'+encoded(body))).decode().rstrip('='))


@pytest.mark.parametrize('state',['committed','sealed_no_commit'])
def test_exact_terminal_finalise_and_replay(state):
    current=PinState(batch());e=evidence(state=state)
    updated=finalise(current,e,now=110)
    assert updated.state==('finalised' if state=='committed' else 'released')
    assert finalise(updated,e,now=111)==updated
    assert current.state=='held'


@pytest.mark.parametrize('field,value',[
    ('tenant_id',uid(20)),('operation_id',uid(20)),('actor_id',uid(20)),('key',uid(20)),
    ('request_hash','d'*64),('object_id',uid(20)),('object_version',2),
])
def test_other_operation_binding_cannot_settle(field,value):
    e=evidence();e=replace(e,identity=replace(e.identity,**{field:value}))
    with pytest.raises(ReceiptFailure) as error:finalise(PinState(batch()),e,now=110)
    assert error.value.status==403


def test_committed_wrong_candidate_is_not_retention_authority():
    with pytest.raises(ReceiptFailure):finalise(PinState(batch()),replace(evidence(),result_ref=uid(99)),now=110)


@pytest.mark.parametrize('now',[150,151,float('nan'),float('inf'),True])
def test_expired_or_invalid_clock_preserves_held_pin(now):
    current=PinState(batch())
    with pytest.raises(ReceiptFailure):finalise(current,evidence(),now=now)
    assert current.state=='held'


def test_terminal_conflict_never_releases_finalised_pin():
    done=finalise(PinState(batch()),evidence(),now=110)
    with pytest.raises(ReceiptFailure) as error:finalise(done,evidence(state='sealed_no_commit'),now=111)
    assert error.value.status==409
    with pytest.raises(ReceiptFailure):finalise(done,replace(evidence(),terminal_digest='d'*64),now=111)


def test_receipt_set_is_frozen_sorted_and_uses_version_and_digest():
    values=[{'id':uid(9),'version':1,'digest':'d'*64},{'id':uid(7),'version':2,'digest':'b'*64}]
    refs=receipt_refs(values);values[0]['digest']='e'*64
    assert [r.id for r in refs]==[uid(7),uid(9)] and refs[1].digest=='d'*64
    assert replace(batch(),receipts=refs).digest!=batch().digest
    with pytest.raises(ValueError):replace(batch(),receipts=tuple(reversed(refs)))
    with pytest.raises(ValueError):receipt_refs([refs[0].wire(),refs[0].wire()])
    with pytest.raises(ValueError):receipt_refs([dict(refs[0].wire(),allow=True)])
    with pytest.raises(ValueError):receipt_refs([{'id':uid(n),'version':1,'digest':'a'*64} for n in range(10,21)])


@pytest.mark.parametrize('values',[(uid(0),1,'a'*64),(uid(1),True,'a'*64),(uid(1),0,'a'*64),(uid(1),1,'A'*64)])
def test_invalid_receipt_identity(values):
    with pytest.raises(ValueError):ReceiptRef(*values)


def test_information_candidate_is_current_claim_and_empty_set_is_valid():
    identity=replace(batch().identity,kind='claim.information',object_kind='claim')
    with pytest.raises(ValueError):PinBatch(identity,uid(99),())
    assert PinBatch(identity,identity.object_id,()).receipts==()


@pytest.mark.parametrize('state',['rejected','unknown','not_found'])
def test_generic_status_is_not_terminal_release_proof(state):
    with pytest.raises(ValueError):replace(evidence(),state=state)


def test_claim_access_requires_exact_actor_claim_versioned_receipt_and_live_authority():
    b=batch();a=ClaimAccess(uid(1),uid(3),uid(6),2,1,b.receipts,150)
    assert a.permits(b.receipts[0],tenant_id=uid(1),actor_id=uid(3),claim_id=uid(6),now=110)
    for key in ('tenant_id','actor_id','claim_id'):
        args=dict(tenant_id=uid(1),actor_id=uid(3),claim_id=uid(6),now=110);args[key]=uid(99)
        assert not a.permits(b.receipts[0],**args)
    assert not a.permits(replace(b.receipts[0],version=3),tenant_id=uid(1),actor_id=uid(3),claim_id=uid(6),now=110)
    assert not a.permits(b.receipts[0],tenant_id=uid(1),actor_id=uid(3),claim_id=uid(6),now=150)


@pytest.mark.parametrize('state',['committed','sealed_no_commit'])
def test_crypto_proof_exact_wire_and_stable_terminal_digest_across_reissue(state):
    b,key,body,trust=proof_fixture(state=state);verifier=ReceiptTerminalVerifier({'test':trust},clock=lambda:110)
    first=verifier.verify(sign(key,body),b)
    second_body=copy.deepcopy(body);second_body.update(issued_at=110,expires_at=160)
    second=verifier.verify_bytes(encoded(sign(key,second_body)),b)
    assert first.terminal_digest==second.terminal_digest
    assert first.expires_at==150 and second.expires_at==160
    current=finalise(PinState(b),first,now=110)
    assert finalise(current,second,now=111)==current


@pytest.mark.parametrize('change',[
    lambda b:b.update(schema_version=True),lambda b:b.update(issuer='other'),lambda b:b.update(audience='other'),
    lambda b:b.update(kid='unknown'),lambda b:b.update(expires_at=110),lambda b:b.update(expires_at=161),
    lambda b:b.update(issued_at=116),lambda b:b.update(issued_at=89),lambda b:b.update(issued_at=True),
    lambda b:b['operation'].update(ledger_revision=True),lambda b:b['operation'].update(completed_at=101),
    lambda b:b['operation']['identity'].update(operation_id=uid(90)),lambda b:b['operation']['identity'].update(key=uid(90)),
    lambda b:b['operation']['identity'].update(request_hash='e'*64),lambda b:b['operation']['identity'].update(object_version=True),
    lambda b:b['operation'].update(result_digest='e'*64),lambda b:b['operation'].update(state='rejected'),
    lambda b:b['operation']['result'].update(code='claim_approved'),lambda b:b['operation'].update(extra=True),
    lambda b:b['operation']['identity'].update(extra=True),lambda b:b['operation']['result'].update(ref=uid(90)),
])
def test_signed_but_invalid_or_mismatched_terminal_is_denied(change):
    b,key,body,trust=proof_fixture();change(body)
    with pytest.raises(ReceiptFailure) as error:ReceiptTerminalVerifier({'test':trust},clock=lambda:110).verify(sign(key,body),b)
    assert error.value.status==403


def test_even_correctly_digested_wrong_candidate_and_result_code_are_denied():
    for field,value in [('ref',uid(90)),('code','claim_approved'),('version',True),('version',99)]:
        b,key,body,trust=proof_fixture();result=body['operation']['result'];result[field]=value
        body['operation']['result_digest']=sha256(b'reimbursement:operation-result:v1:'+encoded(result)).hexdigest()
        with pytest.raises(ReceiptFailure):ReceiptTerminalVerifier({'test':trust},clock=lambda:110).verify(sign(key,body),b)


@pytest.mark.parametrize('previous,current,allowed',[(1,2,True),(7,8,True),(7,7,False),(7,9,False),(9223372036854775807,9223372036854775808,False)])
def test_information_signed_result_requires_exact_next_version(previous,current,allowed):
    b,key,body,trust=proof_fixture()
    identity=replace(b.identity,kind='claim.information',object_kind='claim',object_id=b.claim_id,object_version=previous)
    b=replace(b,identity=identity);body['operation']['identity']=identity.wire()
    result=body['operation']['result'];result.update(code='information_received',version=current)
    body['operation']['result_digest']=sha256(b'reimbursement:operation-result:v1:'+encoded(result)).hexdigest()
    verifier=ReceiptTerminalVerifier({'test':trust},clock=lambda:110)
    if allowed:
        e=verifier.verify(sign(key,body),b)
        assert finalise(PinState(b),e,now=110).claim_version==current
    else:
        with pytest.raises(ReceiptFailure):verifier.verify(sign(key,body),b)


def test_forged_or_malformed_signatures_and_json_are_denied():
    b,key,body,trust=proof_fixture();verifier=ReceiptTerminalVerifier({'test':trust},clock=lambda:110);proof=sign(key,body)
    for signature in ['A'*86,proof['signature']+'=',None]:
        with pytest.raises(ReceiptFailure):verifier.verify(dict(proof,signature=signature),b)
    for raw in [b'{"payload":{},"payload":{}}',b'NaN',b'\xff',b' '*32769]:
        with pytest.raises(ReceiptFailure):verifier.verify_bytes(raw,b)


@pytest.mark.parametrize('change',[lambda k:replace(k,revoked=True),lambda k:replace(k,not_after=110),lambda k:replace(k,not_before=111)])
def test_revoked_expired_or_future_trust_denies(change):
    b,key,body,trust=proof_fixture()
    with pytest.raises(ReceiptFailure):ReceiptTerminalVerifier({'test':change(trust)},clock=lambda:110).verify(sign(key,body),b)


def test_proof_authority_clamps_to_key_expiry_and_registry_is_snapshot():
    b,key,body,trust=proof_fixture();registry={'test':replace(trust,not_after=120)}
    verifier=ReceiptTerminalVerifier(registry,clock=lambda:110);registry.clear()
    assert verifier.verify(sign(key,body),b).expires_at==120


def test_unknown_key_defaults_to_deny():
    b,key,body,_=proof_fixture()
    with pytest.raises(ReceiptFailure):ReceiptTerminalVerifier(clock=lambda:110).verify(sign(key,body),b)
