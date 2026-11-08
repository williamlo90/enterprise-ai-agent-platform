"""Disabled-by-default proposal tested against HTTP fixtures only.

No AUTH-04 action extension, real policy eligibility or pin-release authority.
"""
from datetime import date
import hashlib
import json
from reimbursement_service.modules.access.domain import exact, identifier,number,hash_value
from reimbursement_service.modules.access.domain import AccessFailure,FailureKind


def receipt_set(values):
    if type(values) is not list or len(values)>10:raise ValueError
    for value in values:
        exact(value,{'id','version','digest'})
        identifier(value['id']);number(value['version']);hash_value(value['digest'])
    if len({value['id'] for value in values})!=len(values):raise ValueError
    return sorted(values,key=lambda value:value['id'])


class ProposedClients:
    def __init__(self,client,*,fixture_contract_enabled=False):
        self.client,self.enabled=client,fixture_contract_enabled is True

    async def _request(self,action,obj,data,operation_id,tenant_id,actor_id,deadline,path):
        if not self.enabled:raise AccessFailure(FailureKind.UNAVAILABLE)
        try:
            for value in (operation_id,tenant_id,actor_id):identifier(value)
            exact(obj,{'kind','id','version'});identifier(obj['id']);number(obj['version']);number(deadline)
            if not self.client.clock()<deadline<=self.client.clock()+60:raise ValueError
            body=dict(contract_version=1,operation_id=operation_id,tenant_id=tenant_id,actor_id=actor_id,
                      action=action,object=obj,deadline=deadline,data=data)
            encoded=json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
            body=json.loads(encoded)
            body['request_digest']=hashlib.sha256(b'reimbursement:platform-fixture:v1:'+encoded).hexdigest()
        except (TypeError,ValueError,KeyError):raise AccessFailure(FailureKind.INVALID) from None
        result=await self.client.call(path,body)
        binding={k:v for k,v in body.items() if k not in {'data','deadline'}}
        try:
            exact(result,set(binding)|{'allow','checked_at','expires_at','result'})
            # Explicit integer checks also reject booleans comparing equal to 1.
            number(result['contract_version']);number(result['object']['version'])
            if any(result[k]!=v for k,v in binding.items()):raise ValueError
            number(result['checked_at']);number(result['expires_at'])
            if (type(result['allow']) is not bool or result['checked_at']>self.client.clock()+5
                    or result['expires_at']<=result['checked_at']
                    or not self.client.clock()<result['expires_at']<=min(deadline,result['checked_at']+60)):
                raise ValueError
            if result['allow'] is False:raise AccessFailure(FailureKind.FORBIDDEN)
            if type(result['result']) is not dict:raise ValueError
            return result['result']
        except (TypeError,ValueError,KeyError):raise AccessFailure(FailureKind.PROTOCOL) from None

    async def eligibility(self,*,policy,operation_kind,trip_date,claim_ref,**binding):
        if not self.enabled:raise AccessFailure(FailureKind.UNAVAILABLE)
        try:
            exact(policy,{'id','version','digest'})
            identifier(policy['id']);number(policy['version']);hash_value(policy['digest'])
            if operation_kind not in {'submission','information','decision'}:raise ValueError
            if operation_kind=='submission':
                if claim_ref is not None:raise ValueError
            else:identifier(claim_ref)
            if type(trip_date) is not str or date.fromisoformat(trip_date).isoformat()!=trip_date:raise ValueError
        except (TypeError,ValueError):raise AccessFailure(FailureKind.INVALID) from None
        value=await self._request('policy.eligibility',dict(kind='policy_package',id=policy['id'],version=policy['version']),
            dict(digest=policy['digest'],operation_kind=operation_kind,trip_date=trip_date,claim_ref=claim_ref),
            path='/internal/v1/policy-packages/'+policy['id']+'/eligibility',**binding)
        try:exact(value,{'eligibility_revision'});number(value['eligibility_revision'])
        except (TypeError,ValueError):raise AccessFailure(FailureKind.PROTOCOL) from None
        return value

    async def pin(self,*,claim,receipts,**binding):
        if not self.enabled:raise AccessFailure(FailureKind.UNAVAILABLE)
        try:
            exact(claim,{'id','version'});identifier(claim['id']);number(claim['version'])
            receipts=json.loads(json.dumps(receipt_set(receipts)))
        except (TypeError,ValueError):raise AccessFailure(FailureKind.INVALID) from None
        value=await self._request('receipt.pin',dict(kind='claim',**claim),dict(receipts=receipts),
                                  path='/internal/v1/receipt-pins',**binding)
        try:
            exact(value,{'attestations'})
            if type(value['attestations']) is not list:raise ValueError
            projected=[]
            for row in value['attestations']:
                exact(row,{'id','version','digest','attestation_digest'});hash_value(row['attestation_digest'])
                projected.append({k:v for k,v in row.items() if k!='attestation_digest'})
            if receipt_set(projected)!=receipts:raise ValueError
        except (TypeError,ValueError,KeyError):raise AccessFailure(FailureKind.PROTOCOL) from None
        return value

    async def finalise(self,*,claim,outcome,proof_digest,**binding):
        # Fixture transport only: this method supplies no proof or authority to
        # release a pin. Production composition never enables this client.
        if not self.enabled:raise AccessFailure(FailureKind.UNAVAILABLE)
        try:
            exact(claim,{'id','version'});identifier(claim['id']);number(claim['version']);hash_value(proof_digest)
            if outcome not in {'committed','sealed_no_commit'}:raise ValueError
        except (TypeError,ValueError):raise AccessFailure(FailureKind.INVALID) from None
        value=await self._request('receipt.finalise',dict(kind='claim',**claim),
            dict(outcome=outcome,proof_digest=proof_digest),
            path='/internal/v1/receipt-pins/'+binding['operation_id']+'/finalise',**binding)
        try:
            exact(value,{'state'})
            if value['state']!=('finalised' if outcome=='committed' else 'released'):raise ValueError
        except (TypeError,ValueError):raise AccessFailure(FailureKind.PROTOCOL) from None
        return value
