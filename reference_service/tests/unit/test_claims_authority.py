import asyncio
import base64
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from reimbursement_service.integrations.platform.claims_authority import ClaimsAuthority
from reimbursement_service.modules.claims.ports import ClaimFailure


def uid():return str(uuid4())


def fixture(allow=True,change=lambda claims:None):
    actor=SimpleNamespace(actor_id=uid(),tenant_id=uid(),operation_id=uid(),request_hash='a'*64,expires_at=160)
    # Decision canonical request has no financial material and no receipt pins.
    payload=dict(schema_version=1,action='claim.decide',tenant_id=actor.tenant_id,actor_id=actor.actor_id,operation_id=actor.operation_id,object=dict(kind='claim',id=uid(),version=1),data=dict(revision=1,stage=1,decision='approve',reason=''))
    from reimbursement_service.modules.access.domain import request_hash
    actor.request_hash=request_hash(payload);policy=dict(id=uid(),version=1,digest='b'*64)
    claims=dict(sub=actor.actor_id,tenant_id=actor.tenant_id,operation_id=actor.operation_id,action='claim.decide',request_hash=actor.request_hash,human_receipt_id=uid());change(claims)
    token='header.'+base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')+'.signature'
    events=[]
    class Eligibility:
        async def eligibility(self,*args,**kw):
            events.append('verified_platform_eligibility')
            return SimpleNamespace(allow=allow,policy=policy,operation_id=actor.operation_id,request_hash=actor.request_hash,operation_kind='decision',trip_date='2026-11-06',claim_ref=payload['object']['id'],eligibility_revision=1,checked_at=100,expires_at=150,blocked_reason=None if allow else 'withdrawn')
    class Pins:
        async def pin(self,*args,**kw):pytest.fail('decision must not pin receipts')
    service=ClaimsAuthority(Eligibility(),Pins())
    async def call(context=None):return await service.authorize(payload,actor,operation_key=uid(),candidate_claim_id=payload['object']['id'],policy=policy,trip_date='2026-11-06',claim={},context={'delegation':token} if context is None else context)
    return call,events


def test_actual_eligibility_precedes_receipt_extraction_and_decision_has_no_pin():
    call,events=fixture();permit=asyncio.run(call());value=json.loads(permit.evidence)
    assert events==['verified_platform_eligibility'] and value['receipt_pins']==[] and value['expires_at']==150


def test_withdrawn_policy_holds_before_any_receipt_preparation():
    call,_=fixture(allow=False)
    with pytest.raises(ClaimFailure) as error:asyncio.run(call())
    assert error.value.status==409


def test_receipt_reference_cannot_be_supplied_as_free_request_context():
    call,_=fixture()
    with pytest.raises(ClaimFailure):asyncio.run(call({'delegation':'token','human_receipt_id':uid()}))


def test_receipt_must_come_from_same_original_verified_actor_payload():
    call,_=fixture(change=lambda c:c.update(request_hash='f'*64))
    with pytest.raises(ClaimFailure):asyncio.run(call())
