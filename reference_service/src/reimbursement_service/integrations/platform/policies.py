"""Live policy metadata for the exact original human mutation, without callbacks."""
from dataclasses import dataclass
from datetime import date
import json
from types import MappingProxyType
from reimbursement_service.modules.access.domain import canonical_payload,request_hash,identifier,number,hash_value
from reimbursement_service.modules.access.domain import AccessFailure,FailureKind


REASONS=frozenset({'withdrawn','known_conflict','retired','not_active','range_mismatch','not_selected','source_unavailable','unknown_binding'})


@dataclass(frozen=True)
class Eligibility:
    allow:bool
    policy:dict
    operation_id:str
    request_hash:str
    operation_kind:str
    trip_date:str
    claim_ref:str|None
    eligibility_revision:int
    checked_at:int
    expires_at:int
    blocked_reason:str|None

    def __post_init__(self):object.__setattr__(self,'policy',MappingProxyType(dict(self.policy)))


class PolicyEligibilityClient:
    def __init__(self,client):self.client=client

    async def eligibility(self,delegation,payload,*,policy,operation_kind,trip_date,claim_ref):
        try:
            frozen=json.loads(canonical_payload(payload));digest=request_hash(frozen)
            if type(policy) is not dict or set(policy)!={'id','version','digest'}:raise ValueError
            identifier(policy['id']);number(policy['version']);hash_value(policy['digest'])
            if (type(trip_date) is not str or date.fromisoformat(trip_date).isoformat()!=trip_date
                or operation_kind not in {'submission','information','decision'}):raise ValueError
            if frozen['action']!={'submission':'claim.submit','information':'claim.information','decision':'claim.decide'}[operation_kind]:raise ValueError
            if operation_kind=='submission':
                if claim_ref is not None or frozen['data']['policy']!=policy:raise ValueError
            else:
                identifier(claim_ref)
                if claim_ref!=frozen['object']['id']:raise ValueError
                if operation_kind=='information' and frozen['data']['policy']!=policy:raise ValueError
            body=json.loads(json.dumps(dict(payload=frozen,policy=policy,operation_kind=operation_kind,trip_date=trip_date,claim_ref=claim_ref),allow_nan=False))
        except (ValueError,TypeError,KeyError):raise AccessFailure(FailureKind.INVALID) from None
        result=await self.client.call('/internal/v1/policy-packages/'+body['policy']['id']+'/eligibility',body,delegation=delegation)
        try:
            if type(result) is not dict or set(result)!={
                'allow','policy','operation_id','request_hash','operation_kind','trip_date','claim_ref',
                'eligibility_revision','checked_at','expires_at','blocked_reason'}:raise ValueError
            if type(result['allow']) is not bool or type(result['policy']) is not dict or set(result['policy'])!={'id','version','digest'}:raise ValueError
            identifier(result['policy']['id']);number(result['policy']['version']);hash_value(result['policy']['digest'])
            if (result['policy']!=body['policy'] or result['operation_id']!=frozen['operation_id'] or result['request_hash']!=digest
                or any(result[k]!=body[k] for k in ('operation_kind','trip_date','claim_ref'))):raise ValueError
            for key in ('eligibility_revision','checked_at','expires_at'):number(result[key])
            if (result['checked_at']>self.client.clock()+5 or not result['checked_at']<result['expires_at']<=result['checked_at']+60
                or self.client.clock()>=result['expires_at']):raise ValueError
            if result['allow']:
                if result['blocked_reason'] is not None:raise ValueError
            elif type(result['blocked_reason']) is not str or result['blocked_reason'] not in REASONS:raise ValueError
            return Eligibility(**result)
        except (ValueError,TypeError,KeyError):raise AccessFailure(FailureKind.PROTOCOL) from None
