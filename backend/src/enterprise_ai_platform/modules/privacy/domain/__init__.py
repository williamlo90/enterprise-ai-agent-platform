"""Policy records contain metadata only; missing decisions never become defaults."""
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from uuid import UUID


class PrivacyFailure(RuntimeError):
    def __init__(self,code):
        self.code=code if code in {'invalid','denied','conflict','policy_missing','held','unavailable'} else 'unavailable'
        super().__init__('Privacy operation '+self.code)


class Category(Enum):
    IDENTITY='identity'
    KNOWLEDGE='knowledge'
    CONVERSATIONS='conversations'
    DRAFTS='drafts'
    RECEIPTS='receipts'
    CLAIMS='claims'
    AUDIT='audit'
    RECOVERY='recovery'
    MODEL='model'


STORES=frozenset({'db','index','file','cache','checkpoint','ledger','backup','pins','provider'})
REQUIRED={
    Category.IDENTITY:frozenset({'db','cache','ledger','backup','provider'}),
    Category.KNOWLEDGE:frozenset({'db','index','file','cache','backup','provider'}),
    Category.CONVERSATIONS:frozenset({'db','cache','checkpoint','backup','provider'}),
    Category.DRAFTS:frozenset({'db','cache','checkpoint','ledger','backup'}),
    Category.RECEIPTS:frozenset({'db','file','ledger','pins','backup'}),
    Category.CLAIMS:frozenset({'db','ledger','backup'}),
    Category.AUDIT:frozenset({'db','backup'}),
    Category.RECOVERY:frozenset({'db','ledger','backup'}),
    Category.MODEL:frozenset({'provider','cache','backup'}),
}


def identifier(value):
    if not isinstance(value,str):raise PrivacyFailure('invalid')
    try:
        if str(UUID(value))!=value:raise ValueError
    except (ValueError,AttributeError):raise PrivacyFailure('invalid') from None
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def instant(value):
    return type(value) in (int,float) and math.isfinite(value) and value>=0


@dataclass(frozen=True)
class Rule:
    category:Category
    retention_seconds:int
    regions:tuple[str,...]
    stores:frozenset[str]
    provider_retention_seconds:int
    training_allowed:bool=False

    def validate(self):
        if (type(self.category) is not Category or type(self.retention_seconds) is not int or not 1<=self.retention_seconds<=3155760000
            or type(self.provider_retention_seconds) is not int or not 0<=self.provider_retention_seconds<=self.retention_seconds
            or self.training_allowed is not False or type(self.stores) is not frozenset or self.stores!=REQUIRED[self.category]
            or type(self.regions) is not tuple or not self.regions or len(self.regions)>10
            or len(set(self.regions))!=len(self.regions) or any(not isinstance(v,str) or not 1<=len(v)<=64 or not v.isascii() or not all(c.isalnum() or c=='-' for c in v) for v in self.regions)):
            raise PrivacyFailure('invalid')


@dataclass(frozen=True)
class PolicyDraft:
    tenant_id:str
    revision:int
    expires_at:float
    rules:tuple[Rule,...]

    def validate(self,now):
        identifier(self.tenant_id)
        if (type(self.revision) is not int or self.revision<1 or not instant(now) or not instant(self.expires_at)
            or now>=self.expires_at or type(self.rules) is not tuple or len(self.rules)!=len(Category)
            or any(type(v) is not Rule or type(v.category) is not Category for v in self.rules)
            or {v.category for v in self.rules}!=set(Category)):raise PrivacyFailure('policy_missing')
        for rule in self.rules:rule.validate()


@dataclass(frozen=True)
class Policy:
    tenant_id:str
    revision:int
    approved_by:str
    approved_at:float
    expires_at:float
    rules:tuple[Rule,...]

    def validate(self,now):
        identifier(self.tenant_id);identifier(self.approved_by)
        if (type(self.revision) is not int or self.revision<1 or not instant(now) or not instant(self.approved_at)
            or not instant(self.expires_at) or not self.approved_at<=now<self.expires_at
            or type(self.rules) is not tuple or len(self.rules)!=len(Category) or any(type(v) is not Rule or type(v.category) is not Category for v in self.rules)
            or {v.category for v in self.rules}!=set(Category)):
            raise PrivacyFailure('policy_missing')
        for rule in self.rules:rule.validate()

    def fingerprint(self):
        return digest(dict(tenant=self.tenant_id,revision=self.revision,approved_by=self.approved_by,approved_at=self.approved_at,expires_at=self.expires_at,
            rules=[dict(category=r.category.value,retention=r.retention_seconds,regions=r.regions,stores=sorted(r.stores),provider_retention=r.provider_retention_seconds,training=r.training_allowed) for r in sorted(self.rules,key=lambda r:r.category.value)]))


@dataclass(frozen=True)
class Grant:
    tenant_id:str
    actor_id:str
    subject_id:str
    operation:str
    category:Category
    revision:int
    expires_at:float

    def validate(self,tenant,subject,operation,category,now):
        identifier(self.tenant_id);identifier(self.actor_id);identifier(self.subject_id)
        if (self.tenant_id!=tenant or self.subject_id!=subject or self.operation!=operation or self.category is not category or type(category) is not Category or operation not in {'delete','export'}
            or type(self.revision) is not int or self.revision<1 or not instant(self.expires_at) or not instant(now) or now>=self.expires_at):raise PrivacyFailure('denied')


@dataclass(frozen=True)
class Asset:
    store:str
    resource_id:str
    version:str
    digest:str
    region:str
    created_at:float
    retain_until:float
    unresolved:bool=False

    def validate(self):
        identifier(self.resource_id)
        if (not isinstance(self.store,str) or self.store not in STORES or not isinstance(self.version,str) or not 1<=len(self.version)<=128
            or not isinstance(self.digest,str) or len(self.digest)!=64 or any(c not in '0123456789abcdef' for c in self.digest)
            or not isinstance(self.region,str) or not self.region or not instant(self.created_at)
            or not instant(self.retain_until) or type(self.unresolved) is not bool):raise PrivacyFailure('invalid')

    def record(self):
        return dict(store=self.store,resource_id=self.resource_id,version=self.version,digest=self.digest,region=self.region,created_at=self.created_at,retain_until=self.retain_until,unresolved=self.unresolved)


@dataclass(frozen=True)
class Inventory:
    tenant_id:str
    subject_id:str
    category:Category
    covered_stores:frozenset[str]
    assets:tuple[Asset,...]

    def validate(self,grant,rule):
        if (self.tenant_id!=grant.tenant_id or self.subject_id!=grant.subject_id or self.category is not rule.category
            or type(self.covered_stores) is not frozenset or self.covered_stores!=rule.stores or type(self.assets) is not tuple or len(self.assets)>1000):raise PrivacyFailure('held')
        keys=set()
        for a in self.assets:
            if type(a) is not Asset:raise PrivacyFailure('held')
            a.validate();key=(a.store,a.resource_id)
            if a.store not in self.covered_stores or key in keys:raise PrivacyFailure('held')
            keys.add(key)
