from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
import re
from uuid import UUID
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError


def exact(value,keys):
    if type(value) is not dict or set(value)!=set(keys):raise ValueError('Invalid package shape.')


def identifier(value):
    if type(value) is not str or str(UUID(value))!=value or UUID(value).int==0:raise ValueError('Invalid package identifier.')
    return value


def integer(value,maximum=9223372036854775807,minimum=1):
    if type(value) is not int or not minimum<=value<=maximum:raise ValueError('Invalid package integer.')
    return value


def digest(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):raise ValueError('Invalid package digest.')
    return value


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def canonical_body(body):
    exact(body,{'schema_version','digest','valid_trip_end_from','valid_trip_end_to','timezone','document_versions','rules'})
    if type(body['schema_version']) is not int or body['schema_version']!=1:raise ValueError('Unsupported package schema.')
    digest(body['digest'])
    start,end=body['valid_trip_end_from'],body['valid_trip_end_to']
    for value in [start]+([] if end is None else [end]):
        if type(value) is not str or date.fromisoformat(value).isoformat()!=value:raise ValueError('Invalid package date.')
    if end is not None and end<=start:raise ValueError('Empty package interval.')
    if type(body['timezone']) is not str or len(body['timezone'])>128:raise ValueError('Invalid timezone.')
    try:ZoneInfo(body['timezone'])
    except (ValueError,ZoneInfoNotFoundError):raise ValueError('Invalid timezone.') from None
    documents=body['document_versions']
    if type(documents) is not list or not 1<=len(documents)<=10:raise ValueError('Invalid source refs.')
    for item in documents:
        exact(item,{'document_id','version_id','content_digest'})
        identifier(item['document_id']);identifier(item['version_id']);digest(item['content_digest'])
    if len({v['version_id'] for v in documents})!=len(documents):raise ValueError('Duplicate source version.')
    rules=body['rules'];exact(rules,{'currency','submission_deadline_days','max_total_minor','categories','approval_route'})
    if rules['currency'] not in {'IDR','SGD','USD'}:raise ValueError('Invalid package currency.')
    integer(rules['submission_deadline_days'],3650,0)
    if rules['max_total_minor'] is not None:integer(rules['max_total_minor'])
    categories=rules['categories']
    if type(categories) is not list or not 1<=len(categories)<=100:raise ValueError('Invalid categories.')
    for category in categories:
        exact(category,{'code','receipt_required','max_amount_minor'})
        if (type(category['code']) is not str or not re.fullmatch('[a-z][a-z0-9_]{0,63}',category['code'])
                or type(category['receipt_required']) is not bool):raise ValueError('Invalid category rule.')
        if category['max_amount_minor'] is not None:integer(category['max_amount_minor'])
    if len({c['code'] for c in categories})!=len(categories):raise ValueError('Duplicate categories.')
    route=rules['approval_route']
    if type(route) is not list or not 1<=len(route)<=10:raise ValueError('Invalid approval route.')
    for ordinal,stage in enumerate(route,1):
        exact(stage,{'stage','approver_id','scope_ref'})
        if type(stage['stage']) is not int or stage['stage']!=ordinal or stage['scope_ref']!='reimbursement':raise ValueError('Invalid route stage.')
        identifier(stage['approver_id'])
    value={**body,'document_versions':sorted(documents,key=lambda v:v['version_id']),
           'rules':{**rules,'categories':sorted(categories,key=lambda v:v['code'])}}
    raw=encoded(value)
    if len(raw)>65536:raise ValueError('Package exceeds transport guard.')
    return raw


@dataclass(frozen=True)
class Package:
    tenant_id:str
    package_id:str
    package_version:int
    canonical:bytes

    def __post_init__(self):
        identifier(self.tenant_id);identifier(self.package_id);integer(self.package_version)
        if type(self.canonical) is not bytes:raise ValueError('Invalid package copy.')
        body=json.loads(self.canonical)
        if canonical_body(body)!=self.canonical:raise ValueError('Noncanonical package.')
        if len(encoded(self.content))>65536:raise ValueError('Package copy exceeds transport guard.')
        if self.digest!=body['digest']:raise ValueError('Package digest mismatch.')

    @property
    def content(self):
        body=json.loads(self.canonical);body.pop('digest')
        return dict(tenant_id=self.tenant_id,package_id=self.package_id,package_version=self.package_version,**body)

    @property
    def digest(self):return sha256(b'reimbursement:rule-package:v1:'+encoded(self.content)).hexdigest()


def request_digest(package_id,version,canonical):
    identifier(package_id);integer(version)
    return sha256(b'reimbursement:package-request:v1:'+encoded(dict(package_id=package_id,package_version=version,body=json.loads(canonical)))).hexdigest()
