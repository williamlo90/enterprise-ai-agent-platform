import asyncio
import copy
import hashlib
import json
from uuid import UUID
import pytest
from reimbursement_service.integrations.platform.receipt_pins import ReceiptPinsClient
from reimbursement_service.modules.access.domain import AccessFailure,request_hash
from reimbursement_service.modules.operations.domain import encoded
from reimbursement_service.config import load_settings,ConfigurationError


def uid(n):return str(UUID(int=n))


def fixture(change=None):
    payload=dict(schema_version=1,action='claim.submit',tenant_id=uid(1),actor_id=uid(2),operation_id=uid(3),object=dict(kind='draft',id=uid(4),version=1),data=dict(
        trip_id=uid(5),currency='SGD',items=[dict(id=uid(6),trip_item_id=uid(7),amount_minor=100,receipt_ids=[uid(8)])],total_minor=100,
        receipts=[dict(id=uid(8),version=3,digest='a'*64)],policy=dict(id=uid(9),version=1,digest='b'*64),approval_route=[dict(stage=1,approver_id=uid(10),scope_ref='reimbursement')],destination='reference-reimbursement'))
    class Client:
        def __init__(self):self.calls=[]
        def clock(self):return 1000
        async def call(self,path,body,*,delegation=None):
            self.calls.append((path,copy.deepcopy(body),delegation));p=body['payload'];obj=p['object'];digest=request_hash(p)
            batch=dict(identity=dict(tenant_id=p['tenant_id'],operation_id=p['operation_id'],actor_id=p['actor_id'],caller='platform-dispatcher',kind=p['action'],key=body['key'],request_hash=digest,object_kind=obj['kind'],object_id=obj['id'],object_version=obj['version']),claim_id=body['candidate_claim_id'],receipts=p['data']['receipts'])
            response=dict(batch_digest=hashlib.sha256(b'eai:receipt-pin-batch:v1:'+encoded(batch)).hexdigest(),state='held',expires_at=1040,
                pins=[dict(**r,pin_operation_ref=uid(11),attestation_digest='c'*64,operation_id=p['operation_id'],request_hash=digest,candidate_claim_id=body['candidate_claim_id'],expires_at=1040) for r in p['data']['receipts']])
            if change:change(response)
            return response
    client=Client();return ReceiptPinsClient(client),client,payload


def test_production_client_requests_exact_key_candidate_and_signed_canonical_material():
    pins,client,payload=fixture()
    result=asyncio.run(pins.pin('original-delegation',payload,operation_key=uid(12),candidate_claim_id=uid(13),actor_expires_at=1050))
    assert result['pins'][0]['candidate_claim_id']==uid(13)
    assert client.calls[0][1]['key']==uid(12) and client.calls[0][2]=='original-delegation'


@pytest.mark.parametrize('change',[
    lambda v:v.update(batch_digest='d'*64),lambda v:v.update(expires_at=True),lambda v:v.update(expires_at=1051),lambda v:v.update(state='released'),
    lambda v:v['pins'][0].update(candidate_claim_id=uid(90)),lambda v:v['pins'][0].update(operation_id=uid(90)),
    lambda v:v['pins'][0].update(request_hash='d'*64),lambda v:v['pins'][0].update(version=4),lambda v:v['pins'][0].update(expires_at=1039),
])
def test_wrong_batch_candidate_key_binding_receipt_or_expiry_never_becomes_a_claim_permit(change):
    pins,client,payload=fixture(change)
    with pytest.raises(AccessFailure) as error:asyncio.run(pins.pin('original',payload,operation_key=uid(12),candidate_claim_id=uid(13),actor_expires_at=1050))
    assert error.value.status==503 and len(client.calls)==1


def test_bad_operation_key_fails_before_network():
    pins,client,payload=fixture()
    with pytest.raises(AccessFailure):asyncio.run(pins.pin('original',payload,operation_key='invalid',candidate_claim_id=uid(13),actor_expires_at=1050))
    assert not client.calls


def test_source_claim_access_flag_is_explicit_and_requires_complete_catalog_integration():
    with pytest.raises(ConfigurationError):load_settings(dict(REIMBURSEMENT_DATABASE_URL='fixture',REIMBURSEMENT_CLAIM_ACCESS_ENABLED='true'))
    settings=load_settings(dict(REIMBURSEMENT_DATABASE_URL='fixture',REIMBURSEMENT_CLAIM_ACCESS_ENABLED='true',REIMBURSEMENT_CATALOG_ENABLED='true',REIMBURSEMENT_PLATFORM_ORIGIN='https://platform.test',REIMBURSEMENT_PLATFORM_CREDENTIAL='a'*43,REIMBURSEMENT_DISPATCHER_CREDENTIAL='b'*43))
    assert settings.claim_access_enabled and settings.catalog_enabled
