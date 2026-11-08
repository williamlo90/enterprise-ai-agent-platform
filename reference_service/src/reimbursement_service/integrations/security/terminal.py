"""Versioned Ed25519 statement of a committed immutable ledger record."""
import base64
from dataclasses import dataclass,field
import json
import re
import time
from types import MappingProxyType
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey,Ed25519PublicKey
from reimbursement_service.modules.operations.domain import Terminal,OperationFailure,encoded

DOMAIN=b'reimbursement:terminal-proof:v1:'


@dataclass(frozen=True)
class TrustedKey:
    public_bytes: bytes = field(repr=False)
    not_before: int
    not_after: int
    revoked: bool = False

    def __post_init__(self):
        if (type(self.public_bytes) is not bytes or len(self.public_bytes)!=32
                or type(self.not_before) is not int or type(self.not_after) is not int
                or not 0<=self.not_before<self.not_after or type(self.revoked) is not bool):
            raise ValueError('Invalid terminal proof trust configuration.')


class Signer:
    def __init__(self,*,kid=None,private_bytes=None,clock=time.time):
        self.kid,self.clock=kid,clock
        if (kid is None)!=(private_bytes is None):raise ValueError('Incomplete proof configuration.')
        self._key=None
        if private_bytes is not None:
            if not re.fullmatch('[a-zA-Z0-9_-]{1,64}',kid) or type(private_bytes) is not bytes or len(private_bytes)!=32:raise ValueError('Invalid proof configuration.')
            self._key=Ed25519PrivateKey.from_private_bytes(private_bytes)

    def __repr__(self):return 'Signer(<redacted>)'

    def issue(self,terminal,*,authority_deadline):
        if self._key is None:raise OperationFailure(503)
        now=int(self.clock())
        if type(authority_deadline) is not int or self.clock()>=authority_deadline:raise OperationFailure(503)
        if terminal.completed_at>now+5:raise OperationFailure(503)
        issued_at=max(now,terminal.completed_at)
        expires_at=min(now+60,authority_deadline)
        if expires_at<=issued_at:raise OperationFailure(503)
        body=dict(schema_version=1,issuer='reference-reimbursement',audience='enterprise-ai-platform',
                  kid=self.kid,operation=terminal.wire(),issued_at=issued_at,expires_at=expires_at)
        try:signature=self._key.sign(DOMAIN+encoded(body))
        except Exception:raise OperationFailure(503) from None
        if self.clock()>=body['expires_at']:raise OperationFailure(503)
        return dict(payload=body,signature=base64.urlsafe_b64encode(signature).decode().rstrip('='))


class Verifier:
    def __init__(self,registry=None,*,clock=time.time):
        self.registry=MappingProxyType(dict(registry or {}));self.clock=clock
        if any(not isinstance(k,str) or type(v) is not TrustedKey for k,v in self.registry.items()):raise ValueError('Invalid proof registry.')

    def verify(self,proof,expected_identity,*,allowed_states=frozenset({'committed','sealed_no_commit'})):
        try:
            if type(proof) is not dict or set(proof)!={'payload','signature'}:raise ValueError
            body=proof['payload']
            if type(body) is not dict or set(body)!={'schema_version','issuer','audience','kid','operation','issued_at','expires_at'}:raise ValueError
            if (type(body['schema_version']) is not int or body['schema_version']!=1
                    or body['issuer']!='reference-reimbursement' or body['audience']!='enterprise-ai-platform'):raise ValueError
            now=self.clock();trust=self.registry.get(body['kid'])
            if trust is None or trust.revoked or not trust.not_before<=now<trust.not_after:raise ValueError
            for key in ('issued_at','expires_at'):
                if type(body[key]) is not int:raise ValueError
            if not (0<body['issued_at']<=now+5 and now<body['expires_at']<=body['issued_at']+60
                    and body['issued_at']<body['expires_at'] and trust.not_before<=body['issued_at']<trust.not_after):raise ValueError
            signature=proof['signature']
            if type(signature) is not str or not re.fullmatch('[A-Za-z0-9_-]{86}',signature):raise ValueError
            raw=base64.b64decode(signature+'==',altchars=b'-_',validate=True)
            if base64.urlsafe_b64encode(raw).decode().rstrip('=')!=signature:raise ValueError
            message=encoded(body)
            if len(message)>32768:raise ValueError
            Ed25519PublicKey.from_public_bytes(trust.public_bytes).verify(raw,DOMAIN+message)
            terminal=Terminal.from_wire(body['operation'])
            if (terminal.identity!=expected_identity or terminal.state not in allowed_states
                    or terminal.completed_at>body['issued_at']):raise ValueError
            if self.clock()>=body['expires_at'] or self.clock()>=trust.not_after:raise ValueError
            return terminal
        except Exception:raise OperationFailure(403) from None

    def verify_bytes(self,raw,expected_identity,**kwargs):
        def unique(pairs):
            value={}
            for key,item in pairs:
                if key in value:raise ValueError
                value[key]=item
            return value
        try:
            if type(raw) is not bytes or len(raw)>32768:raise ValueError
            proof=json.loads(raw.decode('utf-8'),object_pairs_hook=unique,
                parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        except (ValueError,UnicodeError):raise OperationFailure(403) from None
        return self.verify(proof,expected_identity,**kwargs)
