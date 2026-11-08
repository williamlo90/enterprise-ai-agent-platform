from datetime import date
from hashlib import sha256
import json
from uuid import UUID


class TripFailure(RuntimeError):
    def __init__(self,status):
        self.status=status if status in {400,401,403,404,409,503} else 503
        super().__init__('Trip catalog unavailable.')


def identifier(value):
    if type(value) is not str or str(UUID(value))!=value or UUID(value).int==0:raise ValueError
    return value


def filters(after,limit):
    if after is not None:identifier(after)
    if type(limit) is not int or not 1<=limit<=50:raise ValueError
    return dict(after=after,limit=limit)


def listing_digest(after,limit):
    return sha256(b'reimbursement:trip-list:v1:'+json.dumps(filters(after,limit),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def page(value,after,limit):
    """Independent exact REF-04 summary DTO; no expense amounts/items in a list."""
    try:
        filters(after,limit)
        if type(value) is not dict or set(value)!={'items','next_cursor'} or type(value['items']) is not list or len(value['items'])>limit:raise ValueError
        for item in value['items']:
            if type(item) is not dict or set(item)!={'id','start_date','end_date','state','employee_ref'}:raise ValueError
            identifier(item['id'])
            for key in ('start_date','end_date'):
                if type(item[key]) is not str or date.fromisoformat(item[key]).isoformat()!=item[key]:raise ValueError
            if item['end_date']<item['start_date'] or item['state']!='completed' or type(item['employee_ref']) is not str:raise ValueError
            if not 1<=len(item['employee_ref'].encode())<=128 or any(ord(c)<32 or ord(c)==127 for c in item['employee_ref']):raise ValueError
        ids=[item['id'] for item in value['items']]
        if ids!=sorted(set(ids)) or (after is not None and any(item<=after for item in ids)):raise ValueError
        if value['next_cursor'] is not None:
            identifier(value['next_cursor'])
            if not ids or value['next_cursor']!=ids[-1]:raise ValueError
        return json.loads(json.dumps(value,allow_nan=False))
    except (ValueError,TypeError,KeyError):raise TripFailure(503) from None
