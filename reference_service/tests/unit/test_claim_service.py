"""Preparation with controlled source ports; SQL effects have a separate probe."""
import asyncio
import copy
from dataclasses import replace
from datetime import datetime,timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import pytest

from reimbursement_service.modules.claims.domain import ClaimFailure,ClaimPermit
from reimbursement_service.modules.claims.service import ClaimsService,strict_packet
from reimbursement_service.modules.access.domain import VerifiedActor,request_hash


def uid(i):return str(UUID(int=i))


class Gate:
    def __init__(self,clock,mutate=None,poison=None):self.clock,self.mutate,self.poison=clock,mutate,poison;self.calls=[]
    async def authorize(self,payload,actor,**kwargs):
        self.calls.append((copy.deepcopy(payload),copy.deepcopy({k:v for k,v in kwargs.items() if k!='context'})))
        frozen=copy.deepcopy(payload)
        if self.poison:self.poison(payload)
        now=int(self.clock());digest=request_hash(frozen);op=frozen['operation_id']
        kind={'claim.submit':'submission','claim.information':'information','claim.decide':'decision'}[frozen['action']]
        elig=dict(allow=True,policy=kwargs['policy'],operation_id=op,request_hash=digest,operation_kind=kind,trip_date=kwargs['trip_date'],
            claim_ref=None if kind=='submission' else frozen['object']['id'],eligibility_revision=1,checked_at=now,expires_at=now+40,blocked_reason=None)
        pins=[dict(**r,pin_operation_ref=uid(40),attestation_digest='c'*64,expires_at=now+30,operation_id=op,request_hash=digest,candidate_claim_id=kwargs['candidate_claim_id']) for r in frozen['data'].get('receipts',[])]
        evidence=dict(request_hash=digest,operation_id=op,operation_key=kwargs['operation_key'],candidate_claim_id=kwargs['candidate_claim_id'],expires_at=now+20,human_receipt_id=uid(50),human_action_ref=uid(51),eligibility=elig,receipt_pins=pins)
        if self.mutate:self.mutate(evidence)
        return ClaimPermit(json.dumps(evidence).encode())


@pytest.fixture
def case():
    now=[int(datetime(2026,5,2,12,tzinfo=timezone.utc).timestamp())]
    clock=lambda:now[0]
    route=[dict(stage=1,approver_id=uid(7),scope_ref='reimbursement'),dict(stage=2,approver_id=uid(8),scope_ref='reimbursement')]
    policy=dict(tenant_id=uid(2),package_id=uid(10),package_version=1,digest='a'*64,
        valid_trip_end_from='2026-01-01',valid_trip_end_to='2027-01-01',timezone='Asia/Singapore',
        rules=dict(currency='SGD',submission_deadline_days=14,max_total_minor=1000,
            categories=[dict(code='travel',receipt_required=True,max_amount_minor=700)],approval_route=route))
    data=dict(trip_id=uid(3),currency='SGD',items=[dict(id=uid(5),trip_item_id=uid(4),amount_minor=500,receipt_ids=[uid(6)])],
        total_minor=500,receipts=[dict(id=uid(6),version=1,digest='b'*64)],policy=dict(id=uid(10),version=1,digest='a'*64),
        approval_route=copy.deepcopy(route),destination='reference-reimbursement')
    payload=dict(schema_version=1,action='claim.submit',tenant_id=uid(2),actor_id=uid(1),operation_id=uid(20),object=dict(kind='draft',id=uid(30),version=1),data=data)
    facts=dict(trip=dict(tenant_id=uid(2),owner_principal_ref=uid(1),id=uid(3),state='completed',end_date='2026-05-01',
        items=[dict(id=uid(4),category='travel',currency='SGD')]),policy=policy,previous_claim_id=None)
    view=dict(tenant_id=uid(2),id=uid(9),owner_id=uid(1),trip_id=uid(3),trip_date='2026-05-01',policy=copy.deepcopy(data['policy']),
        state='pending_approval',version=1,revision=1,active_stage=1,reviewer_id=uid(7),financial=dict(total_minor=500,currency='SGD'),
        items=copy.deepcopy(data['items']),receipts=copy.deepcopy(data['receipts']),
        stages=[dict(stage=s['stage'],approver_id=s['approver_id'],scope_ref=s['scope_ref'],state='pending') for s in route])
    repo=SimpleNamespace(facts=AsyncMock(return_value=facts),read=AsyncMock(return_value=view),relation=AsyncMock(return_value=view),apply=AsyncMock())
    ops=SimpleNamespace(replay=AsyncMock(return_value=None),commit=AsyncMock(return_value={'fixture':'prepared'}))
    def actor(p):return VerifiedActor(p['actor_id'],p['tenant_id'],uid(99),1,p['action'],p['operation_id'],request_hash(p),now[0],now[0]+60,False)
    gate=Gate(clock)
    service=ClaimsService(repo,ops,authority=gate,clock=clock,ids=lambda:uid(9))
    return SimpleNamespace(now=now,clock=clock,payload=payload,facts=facts,view=view,repo=repo,ops=ops,actor=actor,gate=gate,service=service)


def test_default_deny_authority_does_not_create_feature_or_ledger_work(case):
    service=ClaimsService(case.repo,case.ops,clock=case.clock)
    with pytest.raises(ClaimFailure) as error:asyncio.run(service.execute(case.payload,uid(21),case.actor(case.payload)))
    assert error.value.status==503;case.ops.commit.assert_not_called();case.repo.apply.assert_not_called()


def test_candidate_is_stable_across_process_retry_before_any_ledger_record(case):
    original=copy.deepcopy(case.payload)
    candidates=[]
    for _ in range(2):
        service=ClaimsService(case.repo,case.ops,authority=case.gate,clock=case.clock)
        asyncio.run(service.execute(original,uid(21),case.actor(original),context={'candidate_claim_id':uid(80)}))
        packet=strict_packet(case.ops.commit.call_args.args[3].packet)
        candidates.append(packet['claim_id'])
        assert case.gate.calls[-1][1]['candidate_claim_id']==packet['claim_id']==packet['permit']['candidate_claim_id']
        assert packet['permit']['receipt_pins'][0]['candidate_claim_id']==packet['claim_id']!=uid(80)
    assert candidates[0]==candidates[1]


@pytest.mark.parametrize('change',[
    lambda e:e.update(candidate_claim_id=uid(80)),lambda e:e.pop('candidate_claim_id'),
    lambda e:e['receipt_pins'][0].update(candidate_claim_id=uid(80)),lambda e:e['receipt_pins'][0].pop('candidate_claim_id'),
    lambda e:e.update(operation_key=uid(80)),lambda e:e.pop('operation_key'),
])
def test_envelope_and_each_pin_require_exact_server_candidate_before_commit(case,change):
    case.gate.mutate=change
    with pytest.raises(ClaimFailure) as error:asyncio.run(case.service.execute(case.payload,uid(21),case.actor(case.payload)))
    assert error.value.status==503;case.ops.commit.assert_not_called()


def test_preparation_binds_exact_command_clamps_all_authority_and_uses_server_received_time(case):
    original=copy.deepcopy(case.payload);person=case.actor(original)
    asyncio.run(case.service.execute(original,uid(21),person))
    frozen,key,bounded,work=case.ops.commit.call_args.args
    packet=strict_packet(work.packet)
    assert key==uid(21) and frozen==original and bounded.expires_at==case.now[0]+20<person.expires_at
    assert packet['expires_at']==bounded.expires_at==packet['permit']['expires_at']
    assert packet['received_at']==datetime.fromtimestamp(case.now[0],timezone.utc).isoformat()
    assert packet['identity']['request_hash']==request_hash(original) and json.loads(packet['canonical'])==original
    assert work.expected.ref==uid(9) and work.expected.version==1 and work.expected.code=='claim_submitted'
    case.repo.facts.assert_awaited_once_with(uid(3),original['data']['policy'],person,item_ids=(uid(4),))
    case.repo.apply.assert_not_called()  # Preparation itself owns no write transaction.


def test_committed_replay_precedes_deadline_and_all_new_dependency_calls(case):
    case.now[0]=int(datetime(2027,5,2,tzinfo=timezone.utc).timestamp())
    prior={'operation':{'state':'committed','result':{'ref':uid(9),'version':1,'code':'claim_submitted'}}}
    case.ops.replay.return_value=prior
    assert asyncio.run(case.service.execute(case.payload,uid(21),case.actor(case.payload))) is prior
    case.repo.facts.assert_not_called();case.ops.commit.assert_not_called();assert not case.gate.calls


@pytest.mark.parametrize('change,status',[(dict(scope='catalog'),401),(dict(expires_at=1),401),(dict(expires_at=9999999999),401),
    (dict(request_hash='0'*64),403),(dict(actor_id=uid(88)),403),(dict(tenant_id=uid(88)),403),(dict(access_revision=True),401)])
def test_untrusted_actor_binding_fails_before_any_source_or_ledger_access(case,change,status):
    with pytest.raises(ClaimFailure) as error:asyncio.run(case.service.execute(case.payload,uid(21),replace(case.actor(case.payload),**change)))
    assert error.value.status==status;case.ops.replay.assert_not_called();case.repo.facts.assert_not_called();case.ops.commit.assert_not_called()


@pytest.mark.parametrize('kind',['allow','wrong_policy','policy_bool','wrong_op','wrong_hash','wrong_claim','future_checked','long_eligibility','expired_permit',
    'pin_expired','pin_other_op','pin_other_hash','missing_pin_expiry','wrong_file_version','missing_consent','extra_field'])
def test_incomplete_unbound_or_expired_dependency_evidence_never_reaches_commit(case,kind):
    def mutate(e):
        if kind=='allow':e['eligibility']['allow']=False
        elif kind=='wrong_policy':e['eligibility']['policy']['digest']='d'*64
        elif kind=='policy_bool':e['eligibility']['policy']['version']=True
        elif kind=='wrong_op':e['operation_id']=uid(80)
        elif kind=='wrong_hash':e['request_hash']='e'*64
        elif kind=='wrong_claim':e['eligibility']['claim_ref']=uid(80)
        elif kind=='future_checked':e['eligibility']['checked_at']=case.now[0]+6
        elif kind=='long_eligibility':e['eligibility']['expires_at']=case.now[0]+70
        elif kind=='expired_permit':e['expires_at']=case.now[0]
        elif kind=='pin_expired':e['receipt_pins'][0]['expires_at']=case.now[0]-1
        elif kind=='pin_other_op':e['receipt_pins'][0]['operation_id']=uid(80)
        elif kind=='pin_other_hash':e['receipt_pins'][0]['request_hash']='e'*64
        elif kind=='missing_pin_expiry':e['receipt_pins'][0].pop('expires_at')
        elif kind=='wrong_file_version':e['receipt_pins'][0]['version']=2
        elif kind=='missing_consent':e.pop('human_receipt_id')
        else:e['allow']=True
    case.service.authority=Gate(case.clock,mutate)
    with pytest.raises(ClaimFailure) as error:asyncio.run(case.service.execute(case.payload,uid(21),case.actor(case.payload)))
    assert error.value.status==503;case.ops.commit.assert_not_called()


def test_authority_port_cannot_mutate_frozen_command(case):
    def poison(p):p['data']['total_minor']=999;p['operation_id']=uid(80)
    case.service.authority=Gate(case.clock,poison=poison)
    original=copy.deepcopy(case.payload)
    asyncio.run(case.service.execute(case.payload,uid(21),case.actor(case.payload)))
    packet=strict_packet(case.ops.commit.call_args.args[3].packet)
    assert case.payload==original and json.loads(packet['canonical'])==original


def test_dependency_failure_is_sanitized_and_does_not_write(case):
    case.service.authority=SimpleNamespace(authorize=AsyncMock(side_effect=RuntimeError('fictional-private-context')))
    with pytest.raises(ClaimFailure) as error:asyncio.run(case.service.execute(case.payload,uid(21),case.actor(case.payload)))
    assert error.value.status==503 and 'fictional-private-context' not in str(error.value);case.ops.commit.assert_not_called()


def test_information_uses_locked_rules_after_original_deadline_and_resets_approvals(case):
    case.now[0]=int(datetime(2027,5,2,tzinfo=timezone.utc).timestamp())
    case.view.update(state='needs_information',version=2,active_stage=None,reviewer_id=None)
    case.view['stages'][0]['state']='information_requested'
    p=copy.deepcopy(case.payload);p.update(action='claim.information',object={'kind':'claim','id':uid(9),'version':2})
    p['data'].update(revision=1,draft={'id':uid(32),'version':3})
    asyncio.run(case.service.execute(p,uid(21),case.actor(p)))
    work=case.ops.commit.call_args.args[3]
    assert work.expected.version==3 and work.expected.code=='information_received'
    assert case.gate.calls[0][1]['claim']['revision']==1 and 'financial' not in case.gate.calls[0][1]['claim']


def test_decision_uses_current_stage_metadata_and_does_not_reprepare_financial_facts(case):
    p=dict(schema_version=1,action='claim.decide',tenant_id=uid(2),actor_id=uid(7),operation_id=uid(20),
        object={'kind':'claim','id':uid(9),'version':1},data={'revision':1,'stage':1,'decision':'approve','reason':''})
    asyncio.run(case.service.execute(p,uid(21),case.actor(p)))
    work=case.ops.commit.call_args.args[3]
    assert work.expected.version==2 and work.expected.code=='stage_approved'
    assert strict_packet(work.packet)['permit']['receipt_pins']==[];case.repo.facts.assert_not_called()


def test_previous_rejected_claim_link_is_derived_from_repository_not_public_payload(case):
    case.facts['previous_claim_id']=uid(60)
    asyncio.run(case.service.execute(case.payload,uid(21),case.actor(case.payload)))
    assert strict_packet(case.ops.commit.call_args.args[3].packet)['previous_claim_id']==uid(60)
    case.ops.commit.reset_mock();case.ops.replay.reset_mock()
    person=case.actor(case.payload)
    p=copy.deepcopy(case.payload);p['data']['previous_claim_id']=uid(70)
    with pytest.raises(ClaimFailure) as error:asyncio.run(case.service.execute(p,uid(21),person))
    assert error.value.status==400;case.ops.commit.assert_not_called();case.ops.replay.assert_not_called()


def test_read_checks_target_tenant_and_current_relation_again(case):
    p=dict(schema_version=1,action='claim.read',tenant_id=uid(2),actor_id=uid(1),operation_id=uid(20),object={'kind':'claim','id':uid(9),'version':1},data={})
    assert asyncio.run(case.service.read(p,case.actor(p))) is case.view
    case.view['tenant_id']=uid(80)
    with pytest.raises(ClaimFailure) as error:asyncio.run(case.service.read(p,case.actor(p)))
    assert error.value.status==503
