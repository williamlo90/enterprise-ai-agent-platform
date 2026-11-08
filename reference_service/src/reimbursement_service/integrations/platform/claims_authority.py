"""Actual live policy and receipt authority, with no source-service callback."""
import base64
import json
from reimbursement_service.modules.claims.ports import ClaimFailure,ClaimPermit
from reimbursement_service.modules.access.domain import identifier,request_hash
from reimbursement_service.integrations.platform.client import unique_json


class ClaimsAuthority:
    def __init__(self,eligibility,pins):self.eligibility,self.pins=eligibility,pins

    async def authorize(self,payload,actor,*,operation_key,candidate_claim_id,policy,trip_date,claim,context):
        try:
            if type(context) is not dict or set(context)!={'delegation'}:raise ValueError
            token=context['delegation']
            if type(token) is not str or not 1<=len(token)<=16384:raise ValueError
            kind={'claim.submit':'submission','claim.information':'information','claim.decide':'decision'}[payload['action']]
            claim_ref=None if kind=='submission' else candidate_claim_id
            eligible=await self.eligibility.eligibility(token,payload,policy=policy,operation_kind=kind,trip_date=trip_date,claim_ref=claim_ref)
            if eligible.allow is not True:raise ClaimFailure(409)
            # The SAME exact token/payload has just been cryptographically and
            # currently verified by the platform eligibility endpoint. Extract
            # its receipt reference; never accept a browser-supplied receipt ID.
            parts=token.split('.')
            if len(parts)!=3:raise ValueError
            raw=base64.b64decode(parts[1]+'='*(-len(parts[1])%4),altchars=b'-_',validate=True)
            if base64.urlsafe_b64encode(raw).decode().rstrip('=')!=parts[1]:raise ValueError
            signed=json.loads(raw,object_pairs_hook=unique_json,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            receipt=identifier(signed['human_receipt_id'])
            expected=dict(sub=actor.actor_id,tenant_id=actor.tenant_id,operation_id=actor.operation_id,action=payload['action'],request_hash=request_hash(payload))
            if any(signed[k]!=v for k,v in expected.items()):raise ValueError
            expiry=min(actor.expires_at,eligible.expires_at);pins=[]
            if kind!='decision':
                pinned=await self.pins.pin(token,payload,operation_key=operation_key,candidate_claim_id=candidate_claim_id,actor_expires_at=actor.expires_at)
                expiry=min(expiry,pinned['expires_at']);pins=pinned['pins']
            evidence=dict(request_hash=actor.request_hash,operation_id=actor.operation_id,operation_key=operation_key,candidate_claim_id=candidate_claim_id,
                expires_at=expiry,human_receipt_id=receipt,human_action_ref=receipt,
                eligibility=dict(allow=eligible.allow,policy=dict(eligible.policy),operation_id=eligible.operation_id,request_hash=eligible.request_hash,
                    operation_kind=eligible.operation_kind,trip_date=eligible.trip_date,claim_ref=eligible.claim_ref,eligibility_revision=eligible.eligibility_revision,
                    checked_at=eligible.checked_at,expires_at=eligible.expires_at,blocked_reason=eligible.blocked_reason),receipt_pins=pins)
            return ClaimPermit(json.dumps(evidence,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode())
        except ClaimFailure:raise
        except Exception:raise ClaimFailure(503) from None
