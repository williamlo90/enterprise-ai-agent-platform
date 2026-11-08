import base64
import copy
import hashlib
from uuid import uuid4
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from enterprise_ai_platform.integrations.security.flow_terminal import FlowTerminalVerifier
from enterprise_ai_platform.integrations.security.receipt_terminal import TerminalKey
from enterprise_ai_platform.integrations.security.receipt_settlement import SettlementKey
from enterprise_ai_platform.modules.flows.domain import FlowFailure,encoded,candidate


def uid():return str(uuid4())


def fixture(kind='claim.submit',state='committed',mutate=lambda body:None):
    private=Ed25519PrivateKey.generate();public=private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    identity=dict(tenant_id=uid(),operation_id=uid(),actor_id=uid(),caller='platform-dispatcher',kind=kind,key=uid(),request_hash='a'*64,object_kind='draft' if kind=='claim.submit' else 'claim',object_id=uid(),object_version=2)
    result=dict(ref=candidate(identity),version=1 if kind=='claim.submit' else 3,code={'claim.submit':'claim_submitted','claim.information':'information_received','claim.decide':'claim_approved'}[kind])
    if state=='sealed_no_commit':result=dict(ref=None,version=None,code='sealed_no_commit')
    if state=='rejected':result=dict(ref=None,version=None,code='business_duplicate')
    terminal=dict(identity=identity,state=state,result=result,result_digest=hashlib.sha256(b'reimbursement:operation-result:v1:'+encoded(result)).hexdigest(),completed_at=99,ledger_revision=1)
    body=dict(schema_version=1,issuer='reference-reimbursement',audience='enterprise-ai-platform',kid='source',operation=terminal,issued_at=100,expires_at=150)
    mutate(body)
    signature=base64.urlsafe_b64encode(private.sign(b'reimbursement:terminal-proof:v1:'+encoded(body))).decode().rstrip('=')
    verifier=FlowTerminalVerifier({'source':TerminalKey(public,90,200)},SettlementKey('local',b'x'*32,90,200),'c'*64,clock=lambda:100)
    return verifier,dict(operation=copy.deepcopy(body['operation']),proof=dict(payload=body,signature=signature)),identity


@pytest.mark.parametrize('kind',['claim.submit','claim.information','claim.decide'])
def test_all_three_exact_operation_kinds_verify_real_Ed25519_terminal(kind):
    verifier,response,identity=fixture(kind);terminal=verifier.verify(response,identity)
    assert terminal.state=='committed' and terminal.wire()['identity']==identity


@pytest.mark.parametrize('mutate',[
    lambda b:b.update(audience='other'),lambda b:b.update(expires_at=100),lambda b:b.update(issued_at=True),
    lambda b:b['operation'].update(ledger_revision=True),lambda b:b['operation'].update(result_digest='f'*64),
    lambda b:b['operation']['result'].update(ref=uid()),lambda b:b['operation']['result'].update(version=2),
    lambda b:b['operation'].update(completed_at=101),
])
def test_even_valid_signature_cannot_authorize_mismatched_or_expired_material(mutate):
    verifier,response,identity=fixture(mutate=mutate)
    with pytest.raises(FlowFailure):verifier.verify(response,identity)


def test_fake_signature_and_copy_to_other_full_identity_are_denied():
    verifier,response,identity=fixture();bad=copy.deepcopy(response);bad['proof']['signature']='a'*86
    with pytest.raises(FlowFailure):verifier.verify(bad,identity)
    with pytest.raises(FlowFailure):verifier.verify(response,{**identity,'key':uid()})


def test_rejected_is_classified_but_never_minted_as_terminal_settlement():
    verifier,response,identity=fixture(state='rejected');terminal=verifier.verify(response,identity)
    assert terminal.state=='rejected'
    with pytest.raises(FlowFailure):verifier.settlement(terminal,identity,None)


def test_seal_can_settle_without_pin_when_request_was_never_sent():
    verifier,response,identity=fixture(state='sealed_no_commit');terminal=verifier.verify(response,identity)
    envelope,receipt=verifier.settlement(terminal,identity,None)
    assert receipt is None and envelope['material']['terminal']['state']=='sealed_no_commit'


def test_committed_pin_settlement_has_same_identity_terminal_digest_and_candidate():
    verifier,response,identity=fixture();terminal=verifier.verify(response,identity)
    envelope,receipt=verifier.settlement(terminal,identity,dict(identity=identity,claim_id=candidate(identity),receipts=[]))
    assert receipt['material']['terminal']['terminal_digest']==envelope['material']['terminal_digest']
    assert receipt['material']['batch']['identity']==identity
