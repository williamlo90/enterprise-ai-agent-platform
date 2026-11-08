from dataclasses import dataclass,field
import hashlib
import json
import re
from uuid import UUID,NAMESPACE_URL,uuid5


class FlowFailure(RuntimeError):
    def __init__(self,code='unavailable'):
        self.code=code if code in {'invalid','forbidden','stale','held','conflict','unknown','unavailable'} else 'unavailable'
        super().__init__('Workflow unavailable.')


def exact(value,fields):
    if type(value) is not dict or set(value)!=set(fields):raise ValueError('Invalid workflow contract.')


def identifier(value):
    if type(value) is not str or str(UUID(value))!=value or UUID(value).int==0:raise ValueError('Invalid workflow identifier.')
    return value


def integer(value):
    if type(value) is not int or not 0<value<=9223372036854775807:raise ValueError('Invalid workflow integer.')
    return value


def digest(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):raise ValueError('Invalid workflow digest.')
    return value


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')


def candidate(identity):
    if identity['kind']!='claim.submit':return identity['object_id']
    return str(uuid5(NAMESPACE_URL,'urn:enterprise-ai:claim-candidate:v1:'+identity['tenant_id']+':'+identity['operation_id']+':'+identity['key']+':'+identity['request_hash']))


@dataclass(frozen=True)
class VerifiedTerminal:
    packet:bytes=field(repr=False)
    expires_at:int
    def wire(self):return json.loads(self.packet)
    @property
    def state(self):return self.wire()['state']
    @property
    def digest(self):return hashlib.sha256(b'eai:receipt-terminal:v1:'+self.packet).hexdigest()
