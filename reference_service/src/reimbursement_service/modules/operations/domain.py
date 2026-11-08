"""Immutable operation facts. No claim state or human consent is manufactured."""
from dataclasses import asdict,dataclass
from hashlib import sha256
import json
import re
from uuid import UUID


class OperationFailure(RuntimeError):
    def __init__(self,status):
        self.status=status
        super().__init__('Service operation unavailable.')


def uuid(value):
    if type(value) is not str or str(UUID(value))!=value or UUID(value).int==0:raise ValueError('Invalid operation ID.')
    return value


def integer(value):
    if type(value) is not int or not 0<value<=9223372036854775807:raise ValueError('Invalid operation version.')
    return value


def digest(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):raise ValueError('Invalid operation digest.')
    return value


def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


@dataclass(frozen=True)
class Identity:
    tenant_id: str
    operation_id: str
    actor_id: str
    caller: str
    kind: str
    key: str
    request_hash: str
    object_kind: str
    object_id: str
    object_version: int

    def __post_init__(self):
        for value in (self.tenant_id,self.operation_id,self.actor_id,self.key,self.object_id):uuid(value)
        integer(self.object_version);digest(self.request_hash)
        kinds={'claim.submit':'draft','claim.information':'claim','claim.decide':'claim'}
        if self.caller!='platform-dispatcher' or kinds.get(self.kind)!=self.object_kind:raise ValueError('Invalid operation binding.')

    def wire(self):return asdict(self)


@dataclass(frozen=True)
class Result:
    ref: str | None
    version: int | None
    code: str

    def __post_init__(self):
        if (self.ref is None)!=(self.version is None):raise ValueError('Invalid result reference.')
        if self.ref is not None:uuid(self.ref);integer(self.version)
        if type(self.code) is not str or not re.fullmatch('[a-z][a-z0-9_]{0,63}',self.code):raise ValueError('Invalid result code.')

    def wire(self):return asdict(self)

    @property
    def digest(self):return sha256(b'reimbursement:operation-result:v1:'+encoded(self.wire())).hexdigest()


@dataclass(frozen=True)
class Terminal:
    identity: Identity
    state: str
    result: Result
    completed_at: int
    ledger_revision: int = 1

    def __post_init__(self):
        integer(self.completed_at)
        if self.state not in {'committed','rejected','sealed_no_commit'} or type(self.ledger_revision) is not int or self.ledger_revision!=1:raise ValueError('Invalid terminal state.')
        if self.state=='sealed_no_commit' and self.result!=Result(None,None,'sealed_no_commit'):raise ValueError('Invalid seal result.')

    def wire(self):
        return dict(identity=self.identity.wire(),state=self.state,result=self.result.wire(),
                    result_digest=self.result.digest,completed_at=self.completed_at,ledger_revision=1)

    @classmethod
    def from_wire(cls,value):
        if type(value) is not dict or set(value)!={'identity','state','result','result_digest','completed_at','ledger_revision'}:raise ValueError('Invalid terminal shape.')
        result=cls(Identity(**value['identity']),value['state'],Result(**value['result']),value['completed_at'],value['ledger_revision'])
        if result.result.digest!=value['result_digest']:raise ValueError('Invalid result digest.')
        return result
