"""Policy/receiver contract and publish safety; DB authority tested separately."""
import asyncio
import copy
import json
from hashlib import sha256
from pathlib import Path
import sys
from uuid import UUID
import pytest

from enterprise_ai_platform.modules.policies.domain import Package,ReceivedCopy,canonical_body,encoded,request_digest,PolicyFailure,eligibility_response
from enterprise_ai_platform.modules.policies.service import PolicyService
from enterprise_ai_platform.modules.delegation.domain import VerificationReference,VerifiedActor
from enterprise_ai_platform.modules.delegation.ports import DelegationFailure

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'reference_service/src'))
from reimbursement_service.modules.policies import domain as receiver

def uid(n):return str(UUID(int=n))
ORIGIN='https://policy.example.test'
HEADERS={'origin':ORIGIN,'content-type':'application/json','x-csrf-token':'c'*43}

def content():
    return {'schema_version':1,'valid_trip_end_from':'2026-01-01','valid_trip_end_to':'2027-01-01','timezone':'Asia/Singapore',
        'document_versions':[{'document_id':uid(4),'version_id':uid(5),'content_digest':'a'*64}],
        'rules':{'currency':'SGD','submission_deadline_days':7,'max_total_minor':100000,
            'categories':[{'code':'transport','receipt_required':True,'max_amount_minor':50000}],
            'approval_route':[{'stage':1,'approver_id':uid(3),'scope_ref':'reimbursement'}]}}

def body():
    value=content()
    digest=sha256(b'reimbursement:rule-package:v1:'+encoded(dict(tenant_id=uid(1),package_id=uid(2),package_version=1,**value))).hexdigest()
    return dict(**value,digest=digest)

def values():return {'package_id':uid(2),'package_version':1,'key':uid(6),'expected_version':2}

def test_independent_receiver_canonical_digest_and_request_match():
    value=body();raw=canonical_body(value)
    package=Package(uid(1),uid(2),1,raw)
    independent=receiver.Package(uid(1),uid(2),1,receiver.canonical_body(value))
    assert package.canonical==independent.canonical and package.digest==independent.digest
    assert package.digest=='3c88928a103941cbdb7d1df97817c4c7bd43601d67c2f2929421e5d33314e551'
    assert package.content==independent.content
    assert request_digest(uid(2),1,raw)==receiver.request_digest(uid(2),1,independent.canonical)

@pytest.mark.parametrize('kind',['boolean_version','float_limit','empty_period','unknown_rule','duplicate_source','noncanonical_uuid','unknown_currency','empty_route','missing_receipt_rule'])
def test_strict_package_rejects_invalid_business_material(kind):
    value=body()
    if kind=='boolean_version':value['schema_version']=True
    elif kind=='float_limit':value['rules']['max_total_minor']=1.5
    elif kind=='empty_period':value['valid_trip_end_to']=value['valid_trip_end_from']
    elif kind=='unknown_rule':value['rules']['automatically_approve']=True
    elif kind=='duplicate_source':value['document_versions']*=2
    elif kind=='noncanonical_uuid':value['document_versions'][0]['version_id']='5'
    elif kind=='unknown_currency':value['rules']['currency']='XXX'
    elif kind=='empty_route':value['rules']['approval_route']=[]
    else:value['rules']['categories'][0].pop('receipt_required')
    with pytest.raises((ValueError,TypeError)):canonical_body(value)
    with pytest.raises((ValueError,TypeError)):receiver.canonical_body(value)

def test_digest_binds_tenant_package_version_sources_and_rules():
    raw=canonical_body(body())
    for tenant,pid,version in [(uid(9),uid(2),1),(uid(1),uid(9),1),(uid(1),uid(2),2)]:
        with pytest.raises(ValueError):Package(tenant,pid,version,raw)
    changed=body();changed['rules']['submission_deadline_days']=14
    with pytest.raises(ValueError):Package(uid(1),uid(2),1,canonical_body(changed))

class Repository:
    def __init__(self):self.calls=[];self.ack_denied=False
    async def call(self,token,operation,payload):
        assert token!='s'*43 and 's'*43 not in str(payload)
        self.calls.append((operation,copy.deepcopy(payload)))
        if operation=='context':return {'tenant_id':uid(1),'authority_expires_at':105}
        if operation=='dispatch':return {'publisher':{'tenant_id':uid(1),'actor_id':uid(7)},'body':body(),'operation_id':uid(8),'authority_expires_at':105}
        if operation=='ack' and self.ack_denied:raise PolicyFailure(403)
        return {'state':'prepared','authority_expires_at':105}

class Receiver:
    def __init__(self):self.calls=[];self.result={'package_id':uid(2),'package_version':1,'digest':body()['digest'],'received_at':100,'state':'received'};self.error=None
    async def receive(self,*args):
        self.calls.append(args)
        if self.error:raise self.error
        return ReceivedCopy(copy.deepcopy(self.result),'reference-v1','a'*64)

def service(repo,received=None):return PolicyService(repo,ORIGIN,receiver=received,clock=lambda:100)

def test_draft_derives_tenant_digest_and_never_accepts_body_identity():
    repo=Repository();command=dict(values(),expected_version=0,content=content())
    asyncio.run(service(repo).mutate('s'*43,HEADERS,'draft',command))
    assert [op for op,_ in repo.calls]==['context','draft']
    stored=repo.calls[-1][1]
    independent=receiver.Package(uid(1),uid(2),1,receiver.canonical_body(body()))
    assert stored['digest']==body()['digest'] and stored['canonical']==receiver.encoded(independent.content).decode()
    command['content']['tenant_id']=uid(9)
    with pytest.raises(PolicyFailure) as error:asyncio.run(service(repo).mutate('s'*43,HEADERS,'draft',command))
    assert error.value.status==400

def test_received_ack_never_activates_and_preserves_dispatch_intent():
    repo=Repository();received=Receiver()
    result=asyncio.run(service(repo,received).mutate('s'*43,HEADERS,'dispatch',values()))
    assert result['state']=='prepared' and [op for op,_ in repo.calls]==['dispatch','ack']
    assert received.calls[0][2:]==(uid(2),1,uid(6),uid(8))
    assert repo.calls[-1][1]['key']==uid(6) and repo.calls[-1][1]['expected_version']==2

@pytest.mark.parametrize('change',[{'package_id':uid(9)},{'package_version':True},{'digest':'b'*64},{'received_at':True},{'received_at':106},{'state':'active'},{'extra':'untrusted'}])
def test_mismatched_ack_never_settles_or_activates(change):
    repo=Repository();received=Receiver();received.result.update(change)
    with pytest.raises(PolicyFailure) as error:asyncio.run(service(repo,received).mutate('s'*43,HEADERS,'dispatch',values()))
    assert error.value.status==503 and [op for op,_ in repo.calls]==['dispatch']

def test_timeout_keeps_publication_prepared_and_does_not_guess_success():
    repo=Repository();received=Receiver();received.error=TimeoutError('private transport details')
    with pytest.raises(PolicyFailure) as error:asyncio.run(service(repo,received).mutate('s'*43,HEADERS,'dispatch',values()))
    assert error.value.status==503 and 'private' not in str(error.value)
    assert [op for op,_ in repo.calls]==['dispatch']

def test_source_or_publisher_revocation_during_ack_wait_denies_settlement():
    repo=Repository();repo.ack_denied=True;received=Receiver()
    with pytest.raises(PolicyFailure) as error:asyncio.run(service(repo,received).mutate('s'*43,HEADERS,'dispatch',values()))
    assert error.value.status==403 and [op for op,_ in repo.calls]==['dispatch','ack']

def test_unconfigured_receiver_fails_closed_before_ack():
    repo=Repository()
    with pytest.raises(PolicyFailure) as error:asyncio.run(service(repo).mutate('s'*43,HEADERS,'dispatch',values()))
    assert error.value.status==503 and [op for op,_ in repo.calls]==['dispatch']

@pytest.mark.parametrize('headers',[{**HEADERS,'origin':'https://foreign.example.test'},{**HEADERS,'x-csrf-token':'invalid'},{**HEADERS,'content-type':'text/plain'}])
def test_csrf_and_origin_denied_before_repository(headers):
    repo=Repository()
    with pytest.raises(PolicyFailure) as error:asyncio.run(service(repo).mutate('s'*43,headers,'prepare',values()))
    assert error.value.status==403 and repo.calls==[]

def eligibility_values():
    original=json.loads((ROOT/'backend/tests/contracts/delegation_cases.json').read_text(encoding='utf-8'))['canonical_golden_payload']
    original['object']['id']=uid(11)
    return dict(payload=original,policy={'id':uid(2),'version':1,'digest':body()['digest']},operation_kind='decision',trip_date='2026-05-01',claim_ref=uid(11))

class Verification:
    def __init__(self):self.calls=0;self.revoked=False
    async def verify_private(self,*args):
        self.calls+=1
        if self.revoked:raise DelegationFailure(403)
        ref=VerificationReference('fixture',uid(100+self.calls),str(self.calls)*64,sha256(('eai:internal-caller:v1:'+'r'*43).encode()).hexdigest(),'current','a'*64)
        return VerifiedActor({'allow':True,'scope':'actor_only','expires_at':105,'actor_id':uid(7),'tenant_id':uid(1),'operation_id':uid(10),'request_hash':'d'*64},ref)

def metadata(actor,request):
    return dict(allow=True,policy=request['policy'],operation_id=actor['operation_id'],request_hash=actor['request_hash'],operation_kind=request['operation_kind'],trip_date=request['trip_date'],claim_ref=request['claim_ref'],eligibility_revision=1,checked_at=100,expires_at=105,blocked_reason=None)

def test_exact_second_private_reference_follows_locked_binding_await():
    verification=Verification();seen=[]
    class Locked:
        async def verify(self,*args):assert verification.calls==1;return True
    class Repo(Repository):
        async def eligibility(self,actor,request,credential_hash,ref):
            assert verification.calls==2 and ref['jti']==uid(102) and ref['binding_hash']=='2'*64
            assert credential_hash==ref['credential_hash'];seen.append(ref)
            return metadata(actor,request)
    service=PolicyService(Repo(),ORIGIN,verification,locked=Locked(),clock=lambda:100)
    assert asyncio.run(service.eligibility('r'*43,'signed fixture',eligibility_values()))['allow']
    assert len(seen)==1

def test_revocation_during_locked_binding_prevents_policy_rpc():
    verification=Verification()
    class Locked:
        async def verify(self,*args):verification.revoked=True;return True
    class Repo(Repository):
        async def eligibility(self,*args):pytest.fail('revoked authority reached policy RPC')
    service=PolicyService(Repo(),ORIGIN,verification,locked=Locked(),clock=lambda:100)
    with pytest.raises(DelegationFailure):asyncio.run(service.eligibility('r'*43,'signed fixture',eligibility_values()))
    assert verification.calls==2

@pytest.mark.parametrize('change',[{'document_text':'secret'},{'operation_id':uid(99)},{'request_hash':'e'*64},{'allow':1},{'eligibility_revision':True},{'expires_at':200},{'checked_at':106},{'blocked_reason':'untrusted message'}])
def test_eligibility_output_cannot_leak_content_or_change_binding(change):
    actor={'operation_id':uid(10),'request_hash':'d'*64,'expires_at':105};request=eligibility_values()
    value=metadata(actor,request);value.update(change)
    with pytest.raises(PolicyFailure):eligibility_response(value,actor,request,100)

@pytest.mark.parametrize('reply',[None,{}, {'expires_at':'105'},{'expires_at':[]}])
def test_service_validates_malformed_metadata_before_using_expiry(reply):
    class Locked:
        async def verify(self,*args):return True
    class Repo(Repository):
        async def eligibility(self,*args):return reply
    service=PolicyService(Repo(),ORIGIN,Verification(),locked=Locked(),clock=lambda:100)
    with pytest.raises(PolicyFailure) as error:asyncio.run(service.eligibility('r'*43,'signed fixture',eligibility_values()))
    assert error.value.status==503


def test_financial_array_normalization_is_shared_by_both_verifications_and_policy_rpc():
    from test_delegation import financial_payload
    from enterprise_ai_platform.modules.delegation.domain import canonical_payload,request_hash
    original=financial_payload();original['data']['items'].reverse();original['data']['receipts'].reverse()
    request=dict(payload=original,policy=original['data']['policy'],operation_kind='submission',trip_date='2026-05-01',claim_ref=None)
    normalized=json.loads(canonical_payload(original));expected=request_hash(original);seen=[]
    class Verifier(Verification):
        async def verify_private(self,credential,token,payload):
            assert payload==normalized and encoded(payload)==canonical_payload(original)
            seen.append('verify')
            verified=await super().verify_private(credential,token,payload)
            return VerifiedActor({**verified.actor,'request_hash':expected},verified.reference)
    class Repo(Repository):
        async def eligibility(self,actor,values,credential_hash,ref):
            assert values['payload']==normalized and actor['request_hash']==expected
            assert sha256(b'eai:human-action:v1:'+encoded(values['payload'])).hexdigest()==expected
            seen.append('rpc');return metadata(actor,values)
    service=PolicyService(Repo(),ORIGIN,Verifier(),clock=lambda:100)
    assert asyncio.run(service.eligibility('r'*43,'signed fixture',request))['allow']
    assert seen==['verify','verify','rpc']
    for field in ('amount','receipt'):
        changed=copy.deepcopy(normalized)
        if field=='amount':
            changed['data']['items'][0]['amount_minor']+=1;changed['data']['total_minor']+=1
        else:changed['data']['receipts'][0]['digest']='f'*64
        assert request_hash(changed)!=expected
