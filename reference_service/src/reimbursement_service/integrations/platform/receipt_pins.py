"""Production pin/finalise contract over the bounded authenticated platform client."""
import hashlib
import json
from reimbursement_service.modules.access.domain import canonical_payload,request_hash,identifier,number,hash_value,exact,AccessFailure,FailureKind
from reimbursement_service.modules.operations.domain import Terminal,encoded


class ReceiptPinsClient:
    def __init__(self,client):self.client=client

    async def pin(self,delegation,payload,*,operation_key,candidate_claim_id,actor_expires_at):
        try:
            frozen=json.loads(canonical_payload(payload));identifier(operation_key);identifier(candidate_claim_id);number(actor_expires_at)
            if frozen['action'] not in {'claim.submit','claim.information'}:raise ValueError
            if frozen['action']=='claim.information' and candidate_claim_id!=frozen['object']['id']:raise ValueError
            digest=request_hash(frozen);obj=frozen['object']
            batch=dict(identity=dict(tenant_id=frozen['tenant_id'],operation_id=frozen['operation_id'],actor_id=frozen['actor_id'],caller='platform-dispatcher',
                kind=frozen['action'],key=operation_key,request_hash=digest,object_kind=obj['kind'],object_id=obj['id'],object_version=obj['version']),
                claim_id=candidate_claim_id,receipts=frozen['data']['receipts'])
            expected=hashlib.sha256(b'eai:receipt-pin-batch:v1:'+encoded(batch)).hexdigest()
            if not self.client.clock()<actor_expires_at<=self.client.clock()+65:raise ValueError
        except (TypeError,KeyError,ValueError):raise AccessFailure(FailureKind.INVALID) from None
        value=await self.client.call('/internal/v1/receipt-pins',dict(payload=frozen,key=operation_key,candidate_claim_id=candidate_claim_id),delegation=delegation)
        try:
            exact(value,{'batch_digest','state','expires_at','pins'});number(value['expires_at'])
            if value['batch_digest']!=expected or value['state']!='held' or not self.client.clock()<value['expires_at']<=actor_expires_at:raise ValueError
            if type(value['pins']) is not list or len(value['pins'])!=len(batch['receipts']):raise ValueError
            refs=[]
            for pin in value['pins']:
                exact(pin,{'id','version','digest','pin_operation_ref','attestation_digest','operation_id','request_hash','candidate_claim_id','expires_at'})
                identifier(pin['id']);identifier(pin['pin_operation_ref']);number(pin['version']);number(pin['expires_at']);hash_value(pin['digest']);hash_value(pin['attestation_digest'])
                if (pin['operation_id'],pin['request_hash'],pin['candidate_claim_id'],pin['expires_at'])!=(frozen['operation_id'],digest,candidate_claim_id,value['expires_at']):raise ValueError
                refs.append({k:pin[k] for k in ('id','version','digest')})
            if sorted(refs,key=lambda r:r['id'])!=batch['receipts']:raise ValueError
            return json.loads(encoded(value))
        except (TypeError,KeyError,ValueError):raise AccessFailure(FailureKind.PROTOCOL) from None

    async def finalise(self,tenant_id,operation_id,proof):
        try:
            identifier(tenant_id);identifier(operation_id)
            frozen=json.loads(encoded(proof));terminal=Terminal.from_wire(frozen['payload']['operation'])
            if (terminal.identity.tenant_id,terminal.identity.operation_id)!=(tenant_id,operation_id) or terminal.identity.kind not in {'claim.submit','claim.information'}:raise ValueError
            if terminal.state not in {'committed','sealed_no_commit'}:raise ValueError
            number(frozen['payload']['expires_at'])
            if self.client.clock()>=frozen['payload']['expires_at']:raise ValueError
            digest=hashlib.sha256(b'eai:receipt-terminal:v1:'+encoded(terminal.wire())).hexdigest()
        except (TypeError,KeyError,ValueError):raise AccessFailure(FailureKind.INVALID) from None
        value=await self.client.call('/internal/v1/receipt-pins/'+operation_id+'/finalise',dict(tenant_id=tenant_id,proof=frozen))
        try:
            exact(value,{'state','terminal_digest','claim_version','expires_at'});number(value['expires_at'])
            if value['claim_version'] is not None:number(value['claim_version'])
            expected='finalised' if terminal.state=='committed' else 'released'
            if (value['state'],value['terminal_digest'],value['claim_version'])!=(expected,digest,terminal.result.version):raise ValueError
            if not self.client.clock()<value['expires_at']<=frozen['payload']['expires_at']:raise ValueError
            return json.loads(encoded(value))
        except (TypeError,KeyError,ValueError):raise AccessFailure(FailureKind.PROTOCOL) from None
