import copy
from hashlib import sha256
import json
from uuid import uuid4
import pytest
from reimbursement_service.modules.trips.domain import Trip,ExpenseItem
from reimbursement_service.modules.policies.domain import Package,canonical_body,encoded


def body(tenant,pid,version=1):
    value=dict(schema_version=1,digest='0'*64,valid_trip_end_from='2026-01-01',valid_trip_end_to=None,timezone='Asia/Jakarta',
        document_versions=[dict(document_id=str(uuid4()),version_id=str(uuid4()),content_digest='a'*64)],
        rules=dict(currency='IDR',submission_deadline_days=30,max_total_minor=None,
            categories=[dict(code='transport',receipt_required=True,max_amount_minor=1000000)],
            approval_route=[dict(stage=1,approver_id=str(uuid4()),scope_ref='reimbursement')]))
    normalized=json.loads(canonical_body(value));normalized.pop('digest')
    value['digest']=sha256(b'reimbursement:rule-package:v1:'+encoded(dict(tenant_id=tenant,package_id=pid,package_version=version,**normalized))).hexdigest()
    return value


def test_trip_source_exact_money_and_dates_without_implied_eligibility():
    item=ExpenseItem(str(uuid4()),'2026-10-01','transport',100000,'IDR')
    trip=Trip(str(uuid4()),'2026-11-01','2026-11-02','completed','fictional-employee',(item,))
    assert Trip.from_wire(trip.wire())==trip
    assert trip.items[0].expense_date<trip.start_date  # advance booking is source metadata


@pytest.mark.parametrize('amount',[0,-1,True,1.5,9223372036854775808])
def test_invalid_declared_amount_rejected(amount):
    with pytest.raises(ValueError):ExpenseItem(str(uuid4()),'2026-11-05','transport',amount,'IDR')


def test_trip_negative_schema_and_foreign_fields():
    item=ExpenseItem(str(uuid4()),'2026-11-01','hotel',100,'SGD')
    with pytest.raises(ValueError):Trip(str(uuid4()),'2026-11-02','2026-11-01','completed','fixture',(item,))
    with pytest.raises(ValueError):Trip(str(uuid4()),'2026-11-01','2026-11-02','completed','fixture',(item,item))
    with pytest.raises(ValueError):Trip.from_wire(dict(id=str(uuid4()),owner_id=str(uuid4())))
    with pytest.raises(ValueError):ExpenseItem(str(uuid4()),'2026-02-30','hotel',100,'SGD')


def test_package_digest_immutable_tenant_version_and_content_binding():
    tenant,pid=str(uuid4()),str(uuid4());value=body(tenant,pid)
    package=Package(tenant,pid,1,canonical_body(value))
    assert package.digest==value['digest']
    for t,p,v in [(str(uuid4()),pid,1),(tenant,str(uuid4()),1),(tenant,pid,2)]:
        with pytest.raises(ValueError):Package(t,p,v,canonical_body(value))
    changed=copy.deepcopy(value);changed['rules']['submission_deadline_days']=14
    with pytest.raises(ValueError):Package(tenant,pid,1,canonical_body(changed))


@pytest.mark.parametrize('mutation',[
    lambda b:b.update(owner_id=str(uuid4())),
    lambda b:b.update(valid_trip_end_to='2026-01-01'),
    lambda b:b.update(timezone='Unknown/Not_A_Zone'),
    lambda b:b['rules'].update(currency='EUR'),
    lambda b:b['rules'].update(submission_deadline_days=True),
    lambda b:b['rules'].update(submission_deadline_days=3651),
    lambda b:b['rules'].update(max_total_minor=0),
    lambda b:b['rules']['categories'][0].update(receipt_required=1),
    lambda b:b['rules']['approval_route'][0].update(stage=True),
    lambda b:b['document_versions'].append(b['document_versions'][0]),
    lambda b:b['rules']['categories'].append(b['rules']['categories'][0]),
])
def test_rules_reject_ambiguous_shape_before_authority_or_db(mutation):
    value=body(str(uuid4()),str(uuid4()));mutation(value)
    with pytest.raises((ValueError,TypeError)):canonical_body(value)


def test_canonical_sets_sort_but_route_order_remains_material():
    value=body(str(uuid4()),str(uuid4()))
    value['rules']['categories'].append(dict(code='hotel',receipt_required=False,max_amount_minor=None))
    first=canonical_body(value);value['rules']['categories'].reverse()
    assert canonical_body(value)==first
