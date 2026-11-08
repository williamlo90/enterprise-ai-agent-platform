"""Private pin preparation; original signed material precedes short DB work."""
from hashlib import sha256
from dataclasses import replace
import asyncio
import json
import re
import time
from enterprise_ai_platform.modules.delegation.ports import DelegationServicePort,DelegationFailure,VerifiedActor,canonical_payload
from enterprise_ai_platform.modules.receipts.domain import (
    ReceiptFailure,PinIdentity,PinBatch,PinState,TerminalEvidence,receipt_refs,identifier,integer,digest,exact,live,finalise as settle_state,
)
from enterprise_ai_platform.modules.receipts.ports import ReceiptRepository,TerminalVerifier,SettlementIssuer


class ReceiptPinService:
    def __init__(self,repository:ReceiptRepository,delegation:DelegationServicePort=None,*,proofs:TerminalVerifier=None,settlement:SettlementIssuer=None,clock=time.time):
        self.repository,self.delegation,self.clock=repository,delegation,clock
        self.proofs,self.settlement=proofs,settlement

    async def finalise(self,credential,operation_id,values):
        if self.proofs is None or self.settlement is None:raise ReceiptFailure(503)
        try:
            if type(credential) is not str or not re.fullmatch('[A-Za-z0-9_-]{43}',credential):raise ReceiptFailure(401)
            identifier(operation_id);exact(values,{'tenant_id','proof'});identifier(values['tenant_id'])
            # Freeze caller material before the first dependency await.
            raw=json.dumps(values,ensure_ascii=False,allow_nan=False).encode('utf-8')
            if len(raw)>33792:raise ValueError
            frozen=json.loads(raw)
            credential_hash=sha256(('eai:internal-caller:v1:'+credential).encode()).hexdigest()
        except ReceiptFailure:raise
        except Exception:raise ReceiptFailure(400) from None
        try:
            async with asyncio.timeout(8):row=await self.repository.lookup(credential_hash,frozen['tenant_id'],operation_id)
            exact(row,{'batch','state','terminal_digest','claim_version','expires_at'})
            live(row['expires_at'],self.clock());caller_deadline=row['expires_at']
            batch=PinBatch.from_wire(row['batch'])
            if (batch.identity.tenant_id,batch.identity.operation_id)!=(frozen['tenant_id'],operation_id):raise ValueError
            current=PinState(batch,row['state'],row['terminal_digest'],row['claim_version'])
            evidence=self.proofs.verify(frozen['proof'],batch)
            if type(evidence) is not TerminalEvidence:raise ValueError
            evidence=replace(evidence,expires_at=min(evidence.expires_at,caller_deadline))
            expected=settle_state(current,evidence,now=self.clock())
            envelope=self.settlement.issue(batch,evidence)
            async with asyncio.timeout(min(8,evidence.expires_at-self.clock())):
                result=await self.repository.finalise(credential_hash,envelope)
            exact(result,{'state','terminal_digest','claim_version','expires_at'})
            digest(result['terminal_digest'])
            if result['claim_version'] is not None:integer(result['claim_version'])
            if (result['state'],result['terminal_digest'],result['claim_version'])!=(expected.state,expected.terminal_digest,expected.claim_version):raise ValueError
            live(result['expires_at'],self.clock())
            if result['expires_at']>evidence.expires_at:raise ValueError
            return dict(state=result['state'],terminal_digest=result['terminal_digest'],claim_version=result['claim_version'],expires_at=result['expires_at'])
        except ReceiptFailure:raise
        except Exception:raise ReceiptFailure(503) from None

    async def prepare(self,credential,delegation,values):
        if self.delegation is None:raise ReceiptFailure(503)
        try:
            if type(credential) is not str or not re.fullmatch('[A-Za-z0-9_-]{43}',credential):raise ReceiptFailure(401)
            exact(values,{'payload','key','candidate_claim_id'})
            identifier(values['key']);identifier(values['candidate_claim_id'])
            canonical=canonical_payload(values['payload']);payload=json.loads(canonical)
            if payload['action'] not in {'claim.submit','claim.information'}:raise ValueError
            # Candidate is supplied by the authenticated service, never by an
            # unverified public payload. The registry locks its first binding.
            original=payload['object']
            request=sha256(b'eai:human-action:v1:'+canonical_payload(payload)).hexdigest()
            batch=PinBatch(PinIdentity(payload['tenant_id'],payload['operation_id'],payload['actor_id'],'platform-dispatcher',payload['action'],
                values['key'],request,original['kind'],original['id'],original['version']),values['candidate_claim_id'],receipt_refs(payload['data']['receipts']))
        except ReceiptFailure:raise
        except (ValueError,KeyError,TypeError,UnicodeError):raise ReceiptFailure(400) from None
        try:
            async with asyncio.timeout(8):verified=await self.delegation.verify_private(credential,delegation,json.loads(canonical))
            if type(verified) is not VerifiedActor:raise ReceiptFailure(503)
            actor=dict(verified.actor);reference=verified.reference.wire()
            live(actor['expires_at'],self.clock())
            actor_expires=actor['expires_at']
            if (actor.get('allow') is not True or actor.get('scope')!='actor_only'
                    or any(actor[k]!=v for k,v in dict(tenant_id=batch.identity.tenant_id,actor_id=batch.identity.actor_id,
                        action=batch.identity.kind,operation_id=batch.identity.operation_id,request_hash=batch.identity.request_hash).items())
                    or reference['credential_hash']!=sha256(('eai:internal-caller:v1:'+credential).encode()).hexdigest()):raise ReceiptFailure(403)
            # SQL must lock/revalidate this SAME original verification reference
            # and source receipt facts; a detached allow:true is insufficient.
            async with asyncio.timeout(min(8,actor_expires-self.clock())):
                result=await self.repository.prepare(batch,payload,reference,actor)
            exact(result,{'batch_digest','state','expires_at','pins'})
            if result['batch_digest']!=batch.digest or result['state']!='held':raise ValueError
            live(result['expires_at'],self.clock())
            if result['expires_at']>actor_expires:raise ValueError
            if type(result['pins']) is not list or len(result['pins'])!=len(batch.receipts):raise ValueError
            refs=[];pins=[]
            for pin in result['pins']:
                exact(pin,{'id','version','digest','pin_operation_ref','attestation_digest','operation_id','request_hash','candidate_claim_id','expires_at'})
                identifier(pin['pin_operation_ref']);digest(pin['attestation_digest']);integer(pin['expires_at'])
                if (pin['operation_id']!=batch.identity.operation_id or pin['request_hash']!=batch.identity.request_hash
                        or pin['candidate_claim_id']!=batch.claim_id or pin['expires_at']!=result['expires_at']):raise ValueError
                refs.append({k:pin[k] for k in ('id','version','digest')});pins.append(dict(pin))
            if receipt_refs(refs)!=batch.receipts:raise ValueError
            live(result['expires_at'],self.clock())
            return dict(batch_digest=batch.digest,state='held',expires_at=result['expires_at'],pins=sorted(pins,key=lambda p:p['id']))
        except ReceiptFailure:raise
        except DelegationFailure as error:raise ReceiptFailure(error.status) from None
        except Exception:raise ReceiptFailure(503) from None
