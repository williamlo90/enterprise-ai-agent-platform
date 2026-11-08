"""A preview, chat message or model response is never human consent."""
from dataclasses import dataclass,field
import json
import re
from uuid import UUID,NAMESPACE_URL,uuid5


class ActionFailure(RuntimeError):
    def __init__(self,status=503):
        self.status=status if status in {400,401,403,404,409,422,503} else 503
        super().__init__('Action unavailable.')


def exact(value,fields):
    if type(value) is not dict or set(value)!=set(fields):raise ValueError('Invalid action contract.')


def identifier(value):
    if type(value) is not str or str(UUID(value))!=value or UUID(value).int==0:raise ValueError('Invalid action identifier.')
    return value


def integer(value,maximum=9223372036854775807):
    if type(value) is not int or not 1<=value<=maximum:raise ValueError('Invalid action integer.')
    return value


def digest(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):raise ValueError('Invalid action digest.')
    return value


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')


def identifiers(context,key,kind):
    base='urn:enterprise-ai:action:v1:'+context['tenant_id']+':'+context['actor_id']+':'+kind+':'+key
    return dict(operation_id=str(uuid5(NAMESPACE_URL,base)),receipt_id=str(uuid5(NAMESPACE_URL,base+':receipt')))


@dataclass(frozen=True)
class VerifiedDecisionSource:
    evidence:bytes=field(repr=False)
    def wire(self):
        def unique(rows):
            out={}
            for k,v in rows:
                if k in out:raise ValueError
                out[k]=v
            return out
        try:
            if type(self.evidence) is not bytes or not 1<=len(self.evidence)<=16384:raise ValueError
            result=json.loads(self.evidence,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            exact(result,{'credential_hash','key_id','material','proof'});digest(result['credential_hash']);digest(result['proof'])
            return result
        except Exception:raise ActionFailure(503) from None
