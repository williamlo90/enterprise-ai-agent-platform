"""Deterministic business rules; no HTTP/database/AI providers."""
import copy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from uuid import UUID
import pytest

from reimbursement_service.modules.claims.domain import (
    ClaimFailure,ClaimState,Financial,deadline_exclusive,validate_financial,
    initial_state,decide,provide_information,
)


def uid(i):return str(UUID(int=i))


@pytest.fixture
def facts():
    trip=dict(tenant_id=uid(2),owner_principal_ref=uid(1),id=uid(3),state='completed',
        start_date='2026-04-28',end_date='2026-05-01',items=[dict(id=uid(4),category='travel',currency='SGD',declared_amount_minor=1000)])
    policy=dict(tenant_id=uid(2),package_id=uid(10),package_version=1,digest='a'*64,
        valid_trip_end_from='2026-01-01',valid_trip_end_to='2027-01-01',timezone='Asia/Singapore',
        rules=dict(currency='SGD',submission_deadline_days=14,max_total_minor=1000,
            categories=[dict(code='travel',receipt_required=True,max_amount_minor=700)],
            approval_route=[dict(stage=1,approver_id=uid(7),scope_ref='reimbursement'),dict(stage=2,approver_id=uid(8),scope_ref='reimbursement')]))
    data=dict(trip_id=uid(3),currency='SGD',items=[dict(id=uid(5),trip_item_id=uid(4),amount_minor=500,receipt_ids=[uid(6)])],
        total_minor=500,receipts=[dict(id=uid(6),version=1,digest='b'*64)],policy=dict(id=uid(10),version=1,digest='a'*64),
        approval_route=copy.deepcopy(policy['rules']['approval_route']),destination='reference-reimbursement')
    return dict(data=data,tenant_id=uid(2),owner_id=uid(1),trip=trip,policy=policy,received_at=datetime(2026,5,2,tzinfo=timezone.utc))


def validated(facts):return validate_financial(**facts)


def test_structured_employee_amount_is_not_assumed_receipt_or_declared_amount_match(facts):
    value=validated(facts)
    assert value.total_minor==500 and value.expense_ids=={uid(4)} and value.approvers==(uid(7),uid(8))
    facts['data']['items'][0]['amount_minor']=600
    assert value.items[0].amount_minor==500  # Frozen result does not retain caller containers.


@pytest.mark.parametrize('amount',[True,False,0,-1,0.5,float('nan'),9223372036854775808])
def test_integer_minor_units_and_overflow(facts,amount):
    facts['data']['items'][0]['amount_minor']=amount;facts['data']['total_minor']=amount
    with pytest.raises(ClaimFailure) as error:validated(facts)
    assert error.value.status==400


@pytest.mark.parametrize('change', ['trip_owner','trip_tenant','policy_tenant','unknown_item'])
def test_source_relations_cannot_cross_owner_or_tenant(facts,change):
    if change=='trip_owner':facts['trip']['owner_principal_ref']=uid(99)
    elif change=='trip_tenant':facts['trip']['tenant_id']=uid(99)
    elif change=='policy_tenant':facts['policy']['tenant_id']=uid(99)
    else:facts['data']['items'][0]['trip_item_id']=uid(99)
    with pytest.raises(ClaimFailure) as error:validated(facts)
    assert error.value.status==404


@pytest.mark.parametrize('change',['currency','sum','category_limit','total_limit','category','required_receipt','unused_receipt','duplicate_expense','extra_field'])
def test_financial_constraints(facts,change):
    data=facts['data']
    if change=='currency':data['currency']='USD'
    elif change=='sum':data['total_minor']=501
    elif change=='category_limit':data['items'][0]['amount_minor']=701;data['total_minor']=701
    elif change=='total_limit':facts['policy']['rules']['max_total_minor']=499
    elif change=='category':facts['trip']['items'][0]['category']='unapproved'
    elif change=='required_receipt':data['items'][0]['receipt_ids']=[];data['receipts']=[]
    elif change=='unused_receipt':data['receipts'].append(dict(id=uid(20),version=1,digest='c'*64))
    elif change=='duplicate_expense':data['items'].append({**data['items'][0],'id':uid(20)});data['total_minor']=1000
    else:data['eligible']=True
    with pytest.raises(ClaimFailure) as error:validated(facts)
    assert error.value.status==400


def test_business_duplicate_is_trip_item_not_mutable_line_uuid(facts):
    first=validated(facts);facts['data']['items'][0]['id']=uid(21);second=validated(facts)
    assert first.items[0].line_id!=second.items[0].line_id
    assert first.expense_ids==second.expense_ids


@pytest.mark.parametrize('change',['policy','route','range_start','range_end','self_approval'])
def test_locked_policy_route_range_and_self_approval(facts,change):
    if change=='policy':facts['data']['policy']['digest']='f'*64
    elif change=='route':facts['data']['approval_route'][0]['approver_id']=uid(99)
    elif change=='range_start':facts['policy']['valid_trip_end_from']='2026-05-02'
    elif change=='range_end':facts['policy']['valid_trip_end_to']='2026-05-01'
    else:
        facts['data']['approval_route'][0]['approver_id']=uid(1)
        facts['policy']['rules']['approval_route'][0]['approver_id']=uid(1)
    with pytest.raises(ClaimFailure):validated(facts)


def test_tenant_calendar_deadline_inclusive_end_of_day_and_exclusive_next_midnight(facts):
    boundary=deadline_exclusive('2026-05-01',14,'Asia/Singapore')
    assert boundary==datetime(2026,5,15,16,tzinfo=timezone.utc)
    facts['received_at']=boundary-timedelta(microseconds=1);validated(facts)
    for at in (boundary,boundary+timedelta(microseconds=1)):
        facts['received_at']=at
        with pytest.raises(ClaimFailure) as error:validated(facts)
        assert error.value.status==409
    # Calendar days across DST, not N fixed24-hour periods.
    assert deadline_exclusive('2026-03-07',1,'America/New_York')==datetime(2026,3,9,4,tzinfo=timezone.utc)


def test_information_uses_locked_policy_without_new_submission_deadline(facts):
    facts['received_at']=datetime(2027,5,1,tzinfo=timezone.utc)
    with pytest.raises(ClaimFailure):validated(facts)
    facts['data'].update(revision=1,draft={'id':uid(30),'version':2});facts['initial']=False
    assert validated(facts).total_minor==500


def test_sequential_stages_and_terminal_mutation_denial(facts):
    current=initial_state(uid(9),validated(facts))
    first=decide(current,actor_id=uid(7),expected_version=1,revision=1,stage=1,decision='approve',reason='')
    assert first.state=='pending_approval' and first.active_stage==2 and first.stages==('approved','pending')
    last=decide(first,actor_id=uid(8),expected_version=2,revision=1,stage=2,decision='approve',reason='')
    assert last.state=='approved' and last.active_stage is None and last.version==3
    with pytest.raises(ClaimFailure):decide(last,actor_id=uid(8),expected_version=3,revision=1,stage=2,decision='reject',reason='Denied')
    assert current.version==1 and current.stages==('pending','pending')


@pytest.mark.parametrize('values', [dict(actor_id=uid(8)),dict(actor_id=uid(1)),dict(expected_version=2),dict(revision=2),dict(stage=2),dict(decision='reject'),dict(decision='request_information'),dict(expected_version=True)])
def test_wrong_reviewer_stale_material_future_stage_and_missing_reason_fail(facts,values):
    current=initial_state(uid(9),validated(facts))
    args=dict(actor_id=uid(7),expected_version=1,revision=1,stage=1,decision='approve',reason='');args.update(values)
    with pytest.raises(ClaimFailure):decide(current,**args)


def test_rejection_terminates_remaining_stages_without_changing_original(facts):
    current=initial_state(uid(9),validated(facts))
    result=decide(current,actor_id=uid(7),expected_version=1,revision=1,stage=1,decision='reject',reason='Missing business justification')
    assert result.state=='rejected' and result.stages==('rejected','void') and result.active_stage is None
    assert current.state=='pending_approval'


def test_information_revision_resets_all_approvals_and_preserves_locked_material(facts):
    current=initial_state(uid(9),validated(facts))
    current=decide(current,actor_id=uid(7),expected_version=1,revision=1,stage=1,decision='approve',reason='')
    held=decide(current,actor_id=uid(8),expected_version=2,revision=1,stage=2,decision='request_information',reason='Provide more information')
    revised=replace(held.financial,total_minor=600,items=(replace(held.financial.items[0],amount_minor=600),))
    result=provide_information(held,revised,actor_id=uid(1),expected_version=3,revision=1)
    assert result.revision==2 and result.version==4 and result.active_stage==1 and result.stages==('pending','pending')
    assert held.stages==('approved','information_requested') and result.financial.locked_policy==held.financial.locked_policy
    for kwargs in (dict(actor_id=uid(8),expected_version=3,revision=1),dict(actor_id=uid(1),expected_version=2,revision=1)):
        with pytest.raises(ClaimFailure):provide_information(held,revised,**kwargs)
    with pytest.raises(ClaimFailure):provide_information(held,replace(revised,policy_id=uid(99)),actor_id=uid(1),expected_version=3,revision=1)


@pytest.mark.parametrize('state,stages',[('needs_information',('pending','information_requested')),('rejected',('pending','rejected')),('approved',('approved','pending'))])
def test_invalid_persisted_stage_order_cannot_be_constructed(facts,state,stages):
    with pytest.raises(ValueError):ClaimState(uid(9),validated(facts),state,2,1,None,stages)


@pytest.mark.parametrize('reason,allowed',[('x'*2049,True),('x'*4096,True),('x'*4097,False),('  exact reason\n\t ',True),('canonical\x01text',True),('bad\x00text',False),('é'*2048,True),('é'*2049,False)])
def test_decision_reason_matches_canonical_v1_utf8_boundaries(facts,reason,allowed):
    current=initial_state(uid(9),validated(facts))
    args=dict(actor_id=uid(7),expected_version=1,revision=1,stage=1,decision='reject',reason=reason)
    if allowed:assert decide(current,**args).state=='rejected'
    else:
        with pytest.raises(ClaimFailure) as error:decide(current,**args)
        assert error.value.status==400
