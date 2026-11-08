"""Ingress dispatcher credentials and downstream actor verification are distinct."""
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re
import time
from reimbursement_service.modules.access.domain import canonical_payload, request_hash, OBJECTS
from reimbursement_service.modules.access.domain import AccessFailure, FailureKind
from reimbursement_service.modules.access.ports import ActorVerifier


@dataclass(frozen=True)
class Dispatcher:
    caller: str
    credential_digest: str = field(repr=False)
    allowed_actions: frozenset[str]

    def __post_init__(self):
        if (self.caller!='platform-dispatcher'
                or not re.fullmatch(r'[a-f0-9]{64}',self.credential_digest)
                or type(self.allowed_actions) is not frozenset
                or not self.allowed_actions or not self.allowed_actions<=OBJECTS.keys()):
            raise ValueError('Invalid dispatcher configuration.')


def credential_digest(value):
    return hashlib.sha256(b'reimbursement:dispatcher:v1:'+value.encode('ascii')).hexdigest()


class AccessService:
    def __init__(self, verifier: ActorVerifier, dispatcher: Dispatcher, *, clock=time.time):
        self.verifier,self.dispatcher,self.clock=verifier,dispatcher,clock

    async def authorize(self, headers, payload, *, action, object_kind, object_id, object_version):
        # Callers supply the actual route/object expectation, never an action
        # extracted from an untrusted JWT. Feature modules still check relations.
        values={}
        try:
            for key,value in headers:
                key=key.lower()
                if key in {b'authorization',b'x-eai-delegation',b'content-type',b'cookie'}:
                    if key in values: raise ValueError
                    values[key]=value.decode('ascii')
            credential=values.get(b'authorization','').removeprefix('Bearer ')
            if (not values.get(b'authorization','').startswith('Bearer ')
                    or not re.fullmatch(r'[A-Za-z0-9_-]{43}',credential)
                    or not hmac.compare_digest(credential_digest(credential),self.dispatcher.credential_digest)
                    or b'cookie' in values):
                raise ValueError
            token=values.get(b'x-eai-delegation','')
            if not token or len(token)>16384 or any(ord(c)<33 or ord(c)>126 for c in token): raise ValueError
        except (ValueError,UnicodeError,TypeError):
            raise AccessFailure(FailureKind.UNAUTHENTICATED) from None
        if values.get(b'content-type')!='application/json': raise AccessFailure(FailureKind.INVALID)
        try:
            payload=json.loads(canonical_payload(payload))
            expected=dict(kind=object_kind,id=object_id,version=object_version)
            if payload['action']!=action or payload['object']!=expected or action not in self.dispatcher.allowed_actions:
                raise AccessFailure(FailureKind.FORBIDDEN)
        except (ValueError,TypeError,KeyError): raise AccessFailure(FailureKind.INVALID) from None
        actor=await self.verifier.verify(token,payload)
        if (actor.action!=action or actor.request_hash!=request_hash(payload)
                or actor.actor_id!=payload['actor_id'] or actor.tenant_id!=payload['tenant_id']
                or actor.operation_id!=payload['operation_id'] or actor.scope!='actor_only'):
            raise AccessFailure(FailureKind.FORBIDDEN)
        if self.clock()>=actor.expires_at:
            raise AccessFailure(FailureKind.UNAUTHENTICATED)
        return actor


class DefaultDenyScope:
    async def authorize(self,headers,binding):
        # No producer for list/publisher grants exists in AUTH-04's six actions.
        raise AccessFailure(FailureKind.UNAVAILABLE)
