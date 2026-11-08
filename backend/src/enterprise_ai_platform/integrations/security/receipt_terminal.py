"""Verify the reference service's v1 terminal proof before retaining/releasing pins.

This adapter owns its wire contract; it never imports the other application's
source or treats a digest from an HTTP caller as a signature.
"""
import base64
from dataclasses import dataclass,field
from hashlib import sha256
import json
import re
import time
from types import MappingProxyType
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from enterprise_ai_platform.modules.receipts.domain import (
    PinBatch,PinIdentity,TerminalEvidence,ReceiptFailure,exact,integer,digest,encoded,
)


@dataclass(frozen=True)
class TerminalKey:
    public_bytes:bytes=field(repr=False)
    not_before:int
    not_after:int
    revoked:bool=False

    def __post_init__(self):
        if (type(self.public_bytes) is not bytes or len(self.public_bytes)!=32 or type(self.not_before) is not int
                or type(self.not_after) is not int or not 0<=self.not_before<self.not_after or type(self.revoked) is not bool):
            raise ValueError('Invalid terminal trust configuration.')


class ReceiptTerminalVerifier:
    def __init__(self,registry=None,*,clock=time.time):
        values=dict(registry or {})
        if any(type(k) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,64}',k) or type(v) is not TerminalKey for k,v in values.items()):
            raise ValueError('Invalid terminal trust registry.')
        self.registry,self.clock=MappingProxyType(values),clock

    def verify(self,proof,batch):
        try:
            if type(batch) is not PinBatch:raise ValueError
            raw=encoded(proof)
            if len(raw)>32768:raise ValueError
            frozen=json.loads(raw)
            exact(frozen,{'payload','signature'})
            body=frozen['payload'];exact(body,{'schema_version','issuer','audience','kid','operation','issued_at','expires_at'})
            if (type(body['schema_version']) is not int or body['schema_version']!=1
                    or body['issuer']!='reference-reimbursement' or body['audience']!='enterprise-ai-platform'):raise ValueError
            trust=self.registry.get(body['kid']);now=self.clock()
            if trust is None or trust.revoked or not trust.not_before<=now<trust.not_after:raise ValueError
            integer(body['issued_at']);integer(body['expires_at'])
            if not (trust.not_before<=body['issued_at']<trust.not_after and body['issued_at']<=now+5
                    and body['issued_at']<body['expires_at']<=body['issued_at']+60 and now<body['expires_at']):raise ValueError
            signature=frozen['signature']
            if type(signature) is not str or not re.fullmatch('[A-Za-z0-9_-]{86}',signature):raise ValueError
            decoded=base64.b64decode(signature+'==',altchars=b'-_',validate=True)
            if base64.urlsafe_b64encode(decoded).decode().rstrip('=')!=signature:raise ValueError
            Ed25519PublicKey.from_public_bytes(trust.public_bytes).verify(decoded,b'reimbursement:terminal-proof:v1:'+encoded(body))
            terminal=body['operation'];exact(terminal,{'identity','state','result','result_digest','completed_at','ledger_revision'})
            identity=PinIdentity(**terminal['identity'])
            if identity!=batch.identity or type(terminal['ledger_revision']) is not int or terminal['ledger_revision']!=1:raise ValueError
            result=terminal['result'];exact(result,{'ref','version','code'});digest(terminal['result_digest'])
            if sha256(b'reimbursement:operation-result:v1:'+encoded(result)).hexdigest()!=terminal['result_digest']:raise ValueError
            integer(terminal['completed_at'])
            if terminal['completed_at']>body['issued_at']:raise ValueError
            evidence=TerminalEvidence(identity,terminal['state'],result['ref'],result['version'],result['code'],terminal['completed_at'],
                sha256(b'eai:receipt-terminal:v1:'+encoded(terminal)).hexdigest(),min(body['expires_at'],trust.not_after))
            if evidence.state=='committed' and evidence.result_ref!=batch.claim_id:raise ValueError
            if self.clock()>=evidence.expires_at:raise ValueError
            return evidence
        except Exception:raise ReceiptFailure(403) from None

    def verify_bytes(self,raw,batch):
        def unique(pairs):
            value={}
            for key,item in pairs:
                if key in value:raise ValueError
                value[key]=item
            return value
        try:
            if type(raw) is not bytes or len(raw)>32768:raise ValueError
            proof=json.loads(raw.decode('utf-8'),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        except (ValueError,UnicodeError):raise ReceiptFailure(403) from None
        return self.verify(proof,batch)
