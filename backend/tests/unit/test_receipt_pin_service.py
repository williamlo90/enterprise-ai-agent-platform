import asyncio
import copy
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import pytest
from enterprise_ai_platform.modules.delegation.domain import VerifiedActor,VerificationReference,request_hash
from enterprise_ai_platform.modules.delegation.ports import DelegationFailure
from enterprise_ai_platform.modules.receipts.domain import ReceiptFailure
from enterprise_ai_platform.modules.receipts.service import ReceiptPinService


def uid(n):return str(UUID(int=n))


@pytest.fixture
def case():
    now=[1000]
    data=dict(trip_id=uid(10),currency='SGD',items=[dict(id=uid(11),trip_item_id=uid(12),amount_minor=100,receipt_ids=[uid(13)])],
        total_minor=100,receipts=[dict(id=uid(13),version=3,digest='a'*64)],policy=dict(id=uid(14),version=1,digest='b'*64),
        approval_route=[dict(stage=1,approver_id=uid(15),scope_ref='reimbursement')],destination='reference-reimbursement')
    payload=dict(schema_version=1,action='claim.submit',tenant_id=uid(1),actor_id=uid(2),operation_id=uid(3),object=dict(kind='draft',id=uid(4),version=2),data=data)
    values=dict(payload=payload,key=uid(5),candidate_claim_id=uid(6))
    actor=dict(allow=True,scope='actor_only',actor_id=uid(2),tenant_id=uid(1),membership_id=uid(7),access_revision=1,
        action='claim.submit',operation_id=uid(3),request_hash=request_hash(payload),verified_at=1000,expires_at=1050,replayed=False)
    reference=VerificationReference('fixture',uid(8),'c'*64,sha256(('eai:internal-caller:v1:'+'a'*43).encode()).hexdigest(),'test','d'*64)
    delegation=SimpleNamespace(verify_private=AsyncMock(return_value=VerifiedActor(actor,reference)))
    async def prepare(batch,payload,ref,person):
        assert ref==reference.wire() and person==actor and batch.identity.request_hash==request_hash(payload)
        return dict(batch_digest=batch.digest,state='held',expires_at=1040,pins=[dict(**r.wire(),pin_operation_ref=uid(9),attestation_digest='e'*64,
            operation_id=batch.identity.operation_id,request_hash=batch.identity.request_hash,candidate_claim_id=batch.claim_id,expires_at=1040) for r in batch.receipts])
    repository=SimpleNamespace(prepare=AsyncMock(side_effect=prepare))
    service=ReceiptPinService(repository,delegation,clock=lambda:now[0])
    return SimpleNamespace(now=now,payload=payload,values=values,actor=actor,reference=reference,delegation=delegation,repository=repository,service=service,prepare=prepare)


def test_exact_original_verification_reference_and_candidate_reach_repository(case):
    result=asyncio.run(case.service.prepare('a'*43,'controlled',case.values))
    assert result['state']=='held' and result['pins'][0]['candidate_claim_id']==uid(6)
    batch,payload,reference,actor=case.repository.prepare.call_args.args
    assert reference==case.reference.wire() and batch.claim_id==uid(6)
    case.payload['data']['receipts'][0]['digest']='f'*64
    assert payload['data']['receipts'][0]['digest']=='a'*64 and batch.receipts[0].digest=='a'*64


def test_unbound_human_authority_defaults_to_deny(case):
    with pytest.raises(ReceiptFailure) as error:asyncio.run(ReceiptPinService(case.repository).prepare('a'*43,'controlled',case.values))
    assert error.value.status==503;case.repository.prepare.assert_not_called()


def test_verifier_dependency_cannot_mutate_original_material_for_repository(case):
    original=case.delegation.verify_private.return_value
    async def mutate(credential,delegation,payload):
        payload['data']['receipts'][0]['digest']='f'*64
        payload['data']['items'][0]['amount_minor']=900
        return original
    case.delegation.verify_private.side_effect=mutate
    result=asyncio.run(case.service.prepare('a'*43,'controlled',case.values))
    assert result['pins'][0]['digest']=='a'*64
    assert case.repository.prepare.call_args.args[1]['data']['items'][0]['amount_minor']==100


@pytest.mark.parametrize('change',[
    lambda c:c.values.update(extra=True),lambda c:c.values.update(key=uid(0)),lambda c:c.values.update(candidate_claim_id=uid(0)),
    lambda c:c.payload.update(action='claim.read'),lambda c:c.payload['data']['receipts'][0].update(version=True),
])
def test_invalid_input_never_reaches_authority_or_registry(case,change):
    change(case)
    with pytest.raises(ReceiptFailure) as error:asyncio.run(case.service.prepare('a'*43,'controlled',case.values))
    assert error.value.status==400;case.delegation.verify_private.assert_not_called();case.repository.prepare.assert_not_called()


def test_detached_allow_dictionary_does_not_replace_private_verified_reference(case):
    case.delegation.verify_private.return_value=case.actor
    with pytest.raises(ReceiptFailure):asyncio.run(case.service.prepare('a'*43,'controlled',case.values))
    case.repository.prepare.assert_not_called()


@pytest.mark.parametrize('field,value',[('tenant_id',uid(20)),('request_hash','f'*64),('operation_id',uid(20)),('allow',1),('scope','catalog')])
def test_verified_actor_mismatch_never_reaches_registry(case,field,value):
    case.delegation.verify_private.return_value=VerifiedActor({**case.actor,field:value},case.reference)
    with pytest.raises(ReceiptFailure):asyncio.run(case.service.prepare('a'*43,'controlled',case.values))
    case.repository.prepare.assert_not_called()


@pytest.mark.parametrize('change',[
    lambda r:r.update(batch_digest='f'*64),lambda r:r.update(state='released'),lambda r:r.update(expires_at=1051),
    lambda r:r['pins'][0].update(candidate_claim_id=uid(20)),lambda r:r['pins'][0].update(request_hash='f'*64),
    lambda r:r['pins'][0].update(operation_id=uid(20)),lambda r:r['pins'][0].update(expires_at=1039),
    lambda r:r['pins'][0].update(version=4),lambda r:r['pins'].append(copy.deepcopy(r['pins'][0])),
])
def test_registry_response_cannot_change_candidate_receipt_or_authority(case,change):
    async def wrong(*args):
        response=await case.prepare(*args);change(response);return response
    case.repository.prepare.side_effect=wrong
    with pytest.raises(ReceiptFailure) as error:asyncio.run(case.service.prepare('a'*43,'controlled',case.values))
    assert error.value.status==503


def test_expiry_during_repository_await_fails_closed(case):
    async def late(*args):
        result=await case.prepare(*args);case.now[0]=1040;return result
    case.repository.prepare.side_effect=late
    with pytest.raises(ReceiptFailure):asyncio.run(case.service.prepare('a'*43,'controlled',case.values))


def test_dependency_errors_are_sanitized_and_authority_rejection_status_preserved(case):
    case.delegation.verify_private.side_effect=RuntimeError('private-upstream-detail')
    with pytest.raises(ReceiptFailure) as error:asyncio.run(case.service.prepare('a'*43,'controlled',case.values))
    assert 'private' not in str(error.value)
    case.delegation.verify_private.side_effect=DelegationFailure(403)
    with pytest.raises(ReceiptFailure) as error:asyncio.run(case.service.prepare('a'*43,'controlled',case.values))
    assert error.value.status==403
