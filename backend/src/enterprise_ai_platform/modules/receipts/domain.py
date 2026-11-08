"""Receipt facts do not become authority merely because their IDs are valid."""
from dataclasses import asdict,dataclass
from hashlib import sha256
import json
import math
import re
from uuid import UUID


class ReceiptFailure(RuntimeError):
    def __init__(self,status=503):
        self.status=status if status in {400,401,403,404,409,503} else 503
        super().__init__('Receipt operation unavailable.')


def exact(value,fields):
    if type(value) is not dict or set(value)!=set(fields):raise ValueError('Invalid receipt contract.')


def identifier(value):
    if type(value) is not str or str(UUID(value))!=value or UUID(value).int==0:raise ValueError('Invalid identifier.')
    return value


def integer(value):
    if type(value) is not int or not 0<value<=9223372036854775807:raise ValueError('Invalid integer.')
    return value


def digest(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):raise ValueError('Invalid digest.')
    return value


def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def live(expires_at,now):
    integer(expires_at)
    if type(now) not in (int,float) or not math.isfinite(now) or not now<expires_at<=now+65:
        raise ValueError('Authority expired.')


@dataclass(frozen=True)
class ReceiptRef:
    id:str
    version:int
    digest:str

    def __post_init__(self):identifier(self.id);integer(self.version);digest(self.digest)

    def wire(self):return asdict(self)


def receipt_refs(values):
    if type(values) is not list or len(values)>10:raise ValueError('Invalid receipt count.')
    refs=[]
    for row in values:
        exact(row,{'id','version','digest'});refs.append(ReceiptRef(**row))
    if len({r.id for r in refs})!=len(refs):raise ValueError('Duplicate receipt.')
    return tuple(sorted(refs,key=lambda r:r.id))


@dataclass(frozen=True)
class PinIdentity:
    tenant_id:str
    operation_id:str
    actor_id:str
    caller:str
    kind:str
    key:str
    request_hash:str
    object_kind:str
    object_id:str
    object_version:int

    def __post_init__(self):
        for v in (self.tenant_id,self.operation_id,self.actor_id,self.key,self.object_id):identifier(v)
        integer(self.object_version);digest(self.request_hash)
        if self.caller!='platform-dispatcher' or {'claim.submit':'draft','claim.information':'claim'}.get(self.kind)!=self.object_kind:
            raise ValueError('Invalid pin operation.')

    def wire(self):return asdict(self)


@dataclass(frozen=True)
class PinBatch:
    identity:PinIdentity
    claim_id:str
    receipts:tuple[ReceiptRef,...]

    def __post_init__(self):
        if type(self.identity) is not PinIdentity or type(self.receipts) is not tuple:raise ValueError('Invalid pin batch.')
        identifier(self.claim_id)
        if any(type(r) is not ReceiptRef for r in self.receipts) or receipt_refs([r.wire() for r in self.receipts])!=self.receipts:
            raise ValueError('Noncanonical receipt set.')
        if self.identity.kind=='claim.information' and self.claim_id!=self.identity.object_id:raise ValueError('Wrong information claim.')

    def wire(self):return dict(identity=self.identity.wire(),claim_id=self.claim_id,receipts=[r.wire() for r in self.receipts])

    @classmethod
    def from_wire(cls,value):
        exact(value,{'identity','claim_id','receipts'})
        return cls(PinIdentity(**value['identity']),value['claim_id'],receipt_refs(value['receipts']))

    @property
    def digest(self):return sha256(b'eai:receipt-pin-batch:v1:'+encoded(self.wire())).hexdigest()


@dataclass(frozen=True)
class TerminalEvidence:
    """Produced only after cryptographic verification by the trusted adapter.

    The type is a private boundary value, not a public JSON authorization flag.
    Database finalisation independently binds its immutable prepared identity.
    """
    identity:PinIdentity
    state:str
    result_ref:str|None
    result_version:int|None
    result_code:str
    completed_at:int
    terminal_digest:str
    expires_at:int

    def __post_init__(self):
        if type(self.identity) is not PinIdentity:raise ValueError('Invalid terminal identity.')
        integer(self.completed_at);integer(self.expires_at);digest(self.terminal_digest)
        if self.completed_at>=self.expires_at:raise ValueError('Invalid terminal time.')
        if self.state=='committed':
            identifier(self.result_ref);integer(self.result_version)
            if self.result_code!={'claim.submit':'claim_submitted','claim.information':'information_received'}[self.identity.kind]:
                raise ValueError('Wrong committed result.')
            expected=1 if self.identity.kind=='claim.submit' else self.identity.object_version+1
            if self.result_version!=expected:raise ValueError('Wrong committed version transition.')
        elif self.state=='sealed_no_commit':
            if (self.result_ref,self.result_version,self.result_code)!=(None,None,'sealed_no_commit'):raise ValueError('Wrong seal result.')
        else:raise ValueError('Insufficient release evidence.')

    def wire(self):return asdict(self)


def settlement_material(batch,evidence):
    if type(batch) is not PinBatch or type(evidence) is not TerminalEvidence or evidence.identity!=batch.identity:
        raise ValueError('Invalid settlement binding.')
    if evidence.state=='committed' and evidence.result_ref!=batch.claim_id:raise ValueError('Wrong settlement claim.')
    return dict(schema_version=1,batch=batch.wire(),batch_digest=batch.digest,terminal=evidence.wire())


@dataclass(frozen=True)
class PinState:
    batch:PinBatch
    state:str='held'
    terminal_digest:str|None=None
    claim_version:int|None=None

    def __post_init__(self):
        if type(self.batch) is not PinBatch or self.state not in {'held','finalised','released'}:raise ValueError('Invalid pin state.')
        if self.state=='held':
            if self.terminal_digest is not None or self.claim_version is not None:raise ValueError('Held pin has terminal result.')
        else:
            digest(self.terminal_digest)
            if self.state=='finalised':integer(self.claim_version)
            elif self.claim_version is not None:raise ValueError('Released pin has claim version.')


def finalise(current,evidence,*,now):
    if type(current) is not PinState or type(evidence) is not TerminalEvidence:raise ReceiptFailure(503)
    try:live(evidence.expires_at,now)
    except ValueError:raise ReceiptFailure(403) from None
    if current.batch.identity!=evidence.identity:raise ReceiptFailure(403)
    if evidence.state=='committed' and evidence.result_ref!=current.batch.claim_id:raise ReceiptFailure(403)
    target=PinState(current.batch,'finalised' if evidence.state=='committed' else 'released',evidence.terminal_digest,evidence.result_version)
    if current.state!='held' and current!=target:raise ReceiptFailure(409)
    return target


@dataclass(frozen=True)
class ClaimAccess:
    tenant_id:str
    actor_id:str
    claim_id:str
    claim_version:int
    revision:int
    receipts:tuple[ReceiptRef,...]
    expires_at:int

    def __post_init__(self):
        for v in (self.tenant_id,self.actor_id,self.claim_id):identifier(v)
        for v in (self.claim_version,self.revision,self.expires_at):integer(v)
        if type(self.receipts) is not tuple or any(type(r) is not ReceiptRef for r in self.receipts):raise ValueError('Invalid access refs.')
        if receipt_refs([r.wire() for r in self.receipts])!=self.receipts:raise ValueError('Noncanonical access refs.')

    def permits(self,ref,*,tenant_id,actor_id,claim_id,now):
        try:live(self.expires_at,now)
        except ValueError:return False
        return type(ref) is ReceiptRef and (self.tenant_id,self.actor_id,self.claim_id)==(tenant_id,actor_id,claim_id) and ref in self.receipts

    def wire(self):
        return dict(tenant_id=self.tenant_id,actor_id=self.actor_id,claim_id=self.claim_id,claim_version=self.claim_version,
            revision=self.revision,receipts=[r.wire() for r in self.receipts],expires_at=self.expires_at)
