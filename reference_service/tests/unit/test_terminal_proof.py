import copy
from dataclasses import replace
import json
from uuid import uuid4
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from reimbursement_service.integrations.security.terminal import Signer,Verifier,TrustedKey
from reimbursement_service.modules.operations.domain import Identity,Result,Terminal,OperationFailure

NOW=2000000000


def fixture(state='committed'):
    private=Ed25519PrivateKey.generate()
    signer=Signer(kid='fixture-key',private_bytes=private.private_bytes_raw(),clock=lambda:NOW)
    verifier=Verifier({'fixture-key':TrustedKey(private.public_key().public_bytes_raw(),NOW-100,NOW+100)},clock=lambda:NOW)
    ident=Identity(str(uuid4()),str(uuid4()),str(uuid4()),'platform-dispatcher','claim.decide',str(uuid4()),'a'*64,'claim',str(uuid4()),1)
    result=Result(None,None,'sealed_no_commit') if state=='sealed_no_commit' else Result(str(uuid4()),1,'fixture')
    terminal=Terminal(ident,state,result,NOW-1)
    return signer,verifier,terminal


@pytest.mark.parametrize('state',['committed','sealed_no_commit'])
def test_signed_terminal_roundtrip(state):
    signer,verifier,terminal=fixture(state)
    proof=signer.issue(terminal,authority_deadline=NOW+60)
    assert verifier.verify(proof,terminal.identity)==terminal
    assert verifier.verify_bytes(json.dumps(proof).encode(),terminal.identity)==terminal


@pytest.mark.parametrize('field,value',[('audience','wrong'),('issuer','wrong'),('kid','unknown'),
    ('expires_at',NOW),('issued_at',NOW+6),('schema_version',True),('extra',True)])
def test_tampered_signed_field_rejected(field,value):
    signer,verifier,terminal=fixture();proof=signer.issue(terminal,authority_deadline=NOW+60);proof['payload'][field]=value
    with pytest.raises(OperationFailure):verifier.verify(proof,terminal.identity)


@pytest.mark.parametrize('field,value',[('request_hash','b'*64),('operation_id',str(uuid4())),
    ('actor_id',str(uuid4())),('tenant_id',str(uuid4())),('key',str(uuid4())),('object_version',2)])
def test_different_expected_identity_rejected(field,value):
    signer,verifier,terminal=fixture()
    with pytest.raises(OperationFailure):verifier.verify(signer.issue(terminal,authority_deadline=NOW+60),replace(terminal.identity,**{field:value}))


def test_empty_registry_revoked_wrong_key_expiry_and_rejected_not_release():
    signer,verifier,terminal=fixture();proof=signer.issue(terminal,authority_deadline=NOW+60)
    with pytest.raises(OperationFailure):Signer().issue(terminal,authority_deadline=NOW+60)
    with pytest.raises(OperationFailure):Verifier().verify(proof,terminal.identity)
    trust=verifier.registry['fixture-key']
    for key in [replace(trust,revoked=True),replace(trust,public_bytes=Ed25519PrivateKey.generate().public_key().public_bytes_raw())]:
        with pytest.raises(OperationFailure):Verifier({'fixture-key':key},clock=lambda:NOW).verify(proof,terminal.identity)
    with pytest.raises(OperationFailure):Verifier(dict(verifier.registry),clock=lambda:NOW+60).verify(proof,terminal.identity)
    rejected=replace(terminal,state='rejected')
    with pytest.raises(OperationFailure):verifier.verify(signer.issue(rejected,authority_deadline=NOW+60),rejected.identity)


def test_resign_keeps_original_terminal_and_timestamp():
    signer,verifier,terminal=fixture();first=signer.issue(terminal,authority_deadline=NOW+60)
    signer.clock=lambda:NOW+10
    second=signer.issue(terminal,authority_deadline=NOW+60)
    assert first['payload']['operation']==second['payload']['operation']
    assert second['payload']['issued_at']==NOW+10
    assert second['payload']['operation']['completed_at']==NOW-1


def test_duplicate_json_and_embedded_key_rejected():
    signer,verifier,terminal=fixture();proof=signer.issue(terminal,authority_deadline=NOW+60)
    raw=json.dumps(proof)
    raw=raw[:-1]+',"signature":"forged"}'
    with pytest.raises(OperationFailure):verifier.verify_bytes(raw.encode(),terminal.identity)
    proof['payload']['jwk_url']='https://attacker.test'
    with pytest.raises(OperationFailure):verifier.verify(proof,terminal.identity)


def test_proof_expiry_bounded_by_actor_and_signing_clock_checked():
    signer,verifier,terminal=fixture()
    assert signer.issue(terminal,authority_deadline=NOW+2)['payload']['expires_at']==NOW+2
    moments=iter([NOW,NOW,NOW+2])
    signer.clock=lambda:next(moments)
    with pytest.raises(OperationFailure):signer.issue(terminal,authority_deadline=NOW+2)


def test_small_database_clock_skew_does_not_extend_authority_or_lifetime():
    signer,verifier,terminal=fixture()
    terminal=replace(terminal,completed_at=NOW+1)
    proof=signer.issue(terminal,authority_deadline=NOW+30)
    assert proof['payload']['issued_at']==NOW+1
    assert proof['payload']['expires_at']==NOW+30
    assert verifier.verify(proof,terminal.identity)==terminal
    with pytest.raises(OperationFailure):signer.issue(replace(terminal,completed_at=NOW+6),authority_deadline=NOW+30)
