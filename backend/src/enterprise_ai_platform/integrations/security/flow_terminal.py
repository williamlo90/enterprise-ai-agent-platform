"""Verify all three REF03 mutation kinds and mint a private settlement MAC."""
import base64
import hashlib
import hmac
import json
import re
import time
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from enterprise_ai_platform.modules.flows.domain import FlowFailure,VerifiedTerminal,exact,encoded,integer,identifier,digest,candidate
from enterprise_ai_platform.modules.receipts.ports import PinBatch,TerminalEvidence
from enterprise_ai_platform.integrations.security.receipt_settlement import ReceiptSettlement


class FlowTerminalVerifier:
    def __init__(self,registry,key,credential_hash,*,clock=time.time):
        self.registry,self.key,self.credential_hash,self.clock=registry,key,credential_hash,clock
        self.receipts=ReceiptSettlement(key,clock=clock)

    def verify(self,response,identity):
        try:
            frozen=json.loads(encoded(response));exact(frozen,{'operation','proof'})
            proof=frozen['proof'];exact(proof,{'payload','signature'});body=proof['payload']
            exact(body,{'schema_version','issuer','audience','kid','operation','issued_at','expires_at'})
            if type(body['schema_version']) is not int or body['schema_version']!=1 or body['issuer']!='reference-reimbursement' or body['audience']!='enterprise-ai-platform':raise ValueError
            trust=self.registry.get(body['kid']);now=self.clock()
            if trust is None or trust.revoked or not trust.not_before<=now<trust.not_after:raise ValueError
            integer(body['issued_at']);integer(body['expires_at'])
            if not trust.not_before<=body['issued_at']<trust.not_after or body['issued_at']>now+5 or not body['issued_at']<body['expires_at']<=body['issued_at']+60 or now>=body['expires_at']:raise ValueError
            if type(proof['signature']) is not str or not re.fullmatch('[A-Za-z0-9_-]{86}',proof['signature']):raise ValueError
            signature=base64.b64decode(proof['signature']+'==',altchars=b'-_',validate=True)
            if base64.urlsafe_b64encode(signature).decode().rstrip('=')!=proof['signature']:raise ValueError
            Ed25519PublicKey.from_public_bytes(trust.public_bytes).verify(signature,b'reimbursement:terminal-proof:v1:'+encoded(body))
            terminal=body['operation'];exact(terminal,{'identity','state','result','result_digest','completed_at','ledger_revision'})
            if terminal!=frozen['operation'] or terminal['identity']!=identity or type(terminal['ledger_revision']) is not int or terminal['ledger_revision']!=1:raise ValueError
            integer(terminal['completed_at'])
            if terminal['completed_at']>body['issued_at']:raise ValueError
            result=terminal['result'];exact(result,{'ref','version','code'});digest(terminal['result_digest'])
            if hashlib.sha256(b'reimbursement:operation-result:v1:'+encoded(result)).hexdigest()!=terminal['result_digest']:raise ValueError
            if type(result['code']) is not str or not re.fullmatch('[a-z][a-z0-9_]{0,63}',result['code']):raise ValueError
            if terminal['state']=='committed':
                identifier(result['ref']);integer(result['version'])
                codes={'claim.submit':{'claim_submitted'},'claim.information':{'information_received'},'claim.decide':{'stage_approved','claim_approved','claim_rejected','information_requested'}}
                if result['code'] not in codes[identity['kind']] or result['ref']!=candidate(identity) or result['version']!=(1 if identity['kind']=='claim.submit' else identity['object_version']+1):raise ValueError
            elif terminal['state']=='sealed_no_commit':
                if result!=dict(ref=None,version=None,code='sealed_no_commit'):raise ValueError
            elif terminal['state']=='rejected':
                if result['ref'] is not None or result['version'] is not None:raise ValueError
            else:raise ValueError
            expires=min(body['expires_at'],trust.not_after,self.key.not_after)
            if self.clock()>=expires:raise ValueError
            return VerifiedTerminal(encoded(terminal),expires)
        except Exception:raise FlowFailure('invalid') from None

    def settlement(self,terminal,identity,batch):
        try:
            if type(terminal) is not VerifiedTerminal or terminal.wire()['identity']!=identity or terminal.state not in {'committed','sealed_no_commit'}:raise ValueError
            now=self.clock()
            if not self.key.not_before<=now<min(self.key.not_after,terminal.expires_at):raise ValueError
            material=dict(schema_version=1,identity=identity,terminal=terminal.wire(),terminal_digest=terminal.digest,expires_at=terminal.expires_at)
            proof=hmac.new(self.key.material,b'eai:flow-settlement:v1:'+encoded(material),hashlib.sha256).hexdigest()
            receipt=None
            if batch is not None and identity['kind']!='claim.decide':
                pins=PinBatch.from_wire(batch);wire=terminal.wire();result=wire['result']
                if pins.identity.wire()!=identity or pins.claim_id!=candidate(identity):raise ValueError
                evidence=TerminalEvidence(pins.identity,wire['state'],result['ref'],result['version'],result['code'],wire['completed_at'],terminal.digest,terminal.expires_at)
                receipt=self.receipts.issue(pins,evidence)
            return dict(credential_hash=self.credential_hash,key_id=self.key.kid,material=material,proof=proof),receipt
        except Exception:raise FlowFailure('invalid') from None
