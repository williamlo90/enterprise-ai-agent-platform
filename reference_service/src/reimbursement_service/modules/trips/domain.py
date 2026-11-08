from dataclasses import asdict,dataclass
from datetime import date
from hashlib import sha256
import json
import re
from uuid import UUID


def identifier(value):
    if type(value) is not str or str(UUID(value))!=value or UUID(value).int==0:raise ValueError('Invalid trip identifier.')
    return value


def business_date(value):
    if type(value) is not str or date.fromisoformat(value).isoformat()!=value:raise ValueError('Invalid trip date.')
    return value


def amount(value):
    if type(value) is not int or not 0<value<=9223372036854775807:raise ValueError('Invalid declared amount.')
    return value


def listing_digest(after,limit):
    return sha256(b'reimbursement:trip-list:v1:'+json.dumps(dict(after=after,limit=limit),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def summary(value):
    if type(value) is not dict or set(value)!={'id','start_date','end_date','state','employee_ref'}:raise ValueError('Invalid trip summary.')
    identifier(value['id']);business_date(value['start_date']);business_date(value['end_date'])
    if (value['end_date']<value['start_date'] or value['state']!='completed' or type(value['employee_ref']) is not str
            or not 1<=len(value['employee_ref'].encode())<=128
            or any(ord(c)<32 or ord(c)==127 for c in value['employee_ref'])):raise ValueError('Invalid trip summary.')
    return value


@dataclass(frozen=True)
class ExpenseItem:
    id:str
    expense_date:str
    category:str
    declared_amount_minor:int
    currency:str

    def __post_init__(self):
        identifier(self.id);business_date(self.expense_date);amount(self.declared_amount_minor)
        if type(self.category) is not str or not re.fullmatch('[a-z][a-z0-9_]{0,63}',self.category):raise ValueError('Invalid expense category.')
        if self.currency not in {'IDR','SGD','USD'}:raise ValueError('Invalid currency.')


@dataclass(frozen=True)
class Trip:
    id:str
    start_date:str
    end_date:str
    state:str
    employee_ref:str
    items:tuple[ExpenseItem,...]

    def __post_init__(self):
        identifier(self.id);business_date(self.start_date);business_date(self.end_date)
        if self.end_date<self.start_date or self.state!='completed':raise ValueError('Invalid trip state/dates.')
        if (type(self.employee_ref) is not str or not 1<=len(self.employee_ref.encode())<=128
                or any(ord(c)<32 or ord(c)==127 for c in self.employee_ref)):raise ValueError('Invalid employee reference.')
        if (type(self.items) is not tuple or not 1<=len(self.items)<=100 or any(type(i) is not ExpenseItem for i in self.items)
                or len({i.id for i in self.items})!=len(self.items)):raise ValueError('Invalid expense item set.')

    def wire(self):
        return dict(id=self.id,start_date=self.start_date,end_date=self.end_date,state=self.state,
                    employee_ref=self.employee_ref,items=[asdict(i) for i in sorted(self.items,key=lambda v:v.id)])

    @classmethod
    def from_wire(cls,value):
        if type(value) is not dict or set(value)!={'id','start_date','end_date','state','employee_ref','items'}:raise ValueError('Invalid trip shape.')
        return cls(**{**value,'items':tuple(ExpenseItem(**item) for item in value['items'])})
