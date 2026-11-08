from dataclasses import dataclass,field
from hashlib import sha256
import json
import re
from datetime import date
from uuid import UUID


class DraftFailure(RuntimeError):
    def __init__(self,status=503):
        self.status=status if status in {400,401,403,404,409,422,503} else 503
        super().__init__('Draft operation unavailable.')


def exact(value,fields):
    if type(value) is not dict or set(value)!=set(fields):raise ValueError('Invalid draft contract.')


def identifier(value):
    if type(value) is not str or str(UUID(value))!=value or UUID(value).int==0:raise ValueError('Invalid draft identifier.')
    return value


def integer(value,minimum=1,maximum=9223372036854775807):
    if type(value) is not int or not minimum<=value<=maximum:raise ValueError('Invalid draft integer.')
    return value


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')


def editable(value):
    """Allow an incomplete empty draft; preview separately requires valid items."""
    exact(value,{'currency','items','receipts'})
    if value['currency'] not in {'IDR','SGD','USD'} or type(value['items']) is not list or len(value['items'])>100:raise ValueError('Invalid draft content.')
    items=[]
    for item in value['items']:
        exact(item,{'id','trip_item_id','amount_minor','receipt_ids'})
        if type(item['receipt_ids']) is not list or len(item['receipt_ids'])>10:raise ValueError('Invalid receipt set.')
        refs=sorted(identifier(r) for r in item['receipt_ids'])
        if len(set(refs))!=len(refs):raise ValueError('Duplicate receipt.')
        items.append(dict(id=identifier(item['id']),trip_item_id=identifier(item['trip_item_id']),amount_minor=integer(item['amount_minor']),receipt_ids=refs))
    if len({i['id'] for i in items})!=len(items) or len({i['trip_item_id'] for i in items})!=len(items):raise ValueError('Duplicate expense.')
    if sum(i['amount_minor'] for i in items)>9223372036854775807:raise ValueError('Draft amount overflow.')
    if type(value['receipts']) is not list or len(value['receipts'])>10:raise ValueError('Invalid receipt set.')
    refs=[]
    for r in value['receipts']:
        exact(r,{'id','version'});refs.append(dict(id=identifier(r['id']),version=integer(r['version'])))
    if len({r['id'] for r in refs})!=len(refs) or {r['id'] for r in refs}!={r for i in items for r in i['receipt_ids']}:raise ValueError('Receipt links do not match.')
    result=dict(currency=value['currency'],items=sorted(items,key=lambda i:i['id']),receipts=sorted(refs,key=lambda r:r['id']))
    if len(encoded(result))>65536:raise ValueError('Draft content too large.')
    return result


def material_hash(value):return sha256(b'eai:draft-material:v1:'+encoded(value)).hexdigest()


def source_trip(value,trip_id):
    exact(value,{'id','start_date','end_date','state','employee_ref','items'})
    if value['id']!=trip_id or value['state']!='completed' or type(value['items']) is not list or not 1<=len(value['items'])<=100:raise ValueError
    for k in ('start_date','end_date'):
        if type(value[k]) is not str or date.fromisoformat(value[k]).isoformat()!=value[k]:raise ValueError
    if value['end_date']<value['start_date'] or type(value['employee_ref']) is not str or not 1<=len(value['employee_ref'].encode())<=128 or any(ord(c)<32 or ord(c)==127 for c in value['employee_ref']):raise ValueError
    seen=set()
    for i in value['items']:
        exact(i,{'id','expense_date','category','declared_amount_minor','currency'})
        identifier(i['id']);integer(i['declared_amount_minor'])
        if i['id'] in seen or i['currency'] not in {'IDR','SGD','USD'} or type(i['category']) is not str or not re.fullmatch('[a-z][a-z0-9_]{0,63}',i['category']):raise ValueError
        # A booking/prepayment expense may precede travel. The authoritative
        # trip owns the item; do not invent an expense-date-in-trip restriction.
        if type(i['expense_date']) is not str or date.fromisoformat(i['expense_date']).isoformat()!=i['expense_date']:raise ValueError
        seen.add(i['id'])
    return json.loads(encoded(value))


@dataclass(frozen=True)
class VerifiedDraftSource:
    """Private source-checked and MACed evidence, never a browser allow flag."""
    evidence:bytes=field(repr=False)

    def wire(self):
        def unique(rows):
            out={}
            for k,v in rows:
                if k in out:raise ValueError
                out[k]=v
            return out
        try:
            if type(self.evidence) is not bytes or not 1<=len(self.evidence)<=65536:raise ValueError
            result=json.loads(self.evidence,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            exact(result,{'credential_hash','key_id','material','proof'})
            if any(type(result[k]) is not str or not re.fullmatch('[a-f0-9]{64}',result[k]) for k in ('credential_hash','proof')):raise ValueError
            return result
        except Exception:raise DraftFailure(503) from None
