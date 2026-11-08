"""Forward v2 policy calculations only. No storage mutation or approval issuer."""
from dataclasses import dataclass
from enum import Enum
import math
from enterprise_ai_platform.modules.privacy.domain import Category,REQUIRED,PrivacyFailure,identifier,digest

MAX_SECONDS=3155760000
MAX_TIME=253402300799


class CopyClass(Enum):
    CONTENT='content'
    CANONICAL='canonical_evidence'
    AUDIT='audit_metadata'
    CONFIG='configuration'
    INDEX='derived_index'
    CACHE='derived_cache'
    CHECKPOINT='checkpoint_state'
    BACKUP='backup_snapshot'
    PROVIDER='provider_copy'
    EXPORT='export_artifact'


class ClockBasis(Enum):
    CREATED='created_at'
    COPY_CREATED='copy_created_at'


class Mode(Enum):
    REQUEST_DELETE='request_delete'
    SCHEDULED_EXPIRY='scheduled_expiry'


ALLOWED_CLASSES={
    'db':frozenset({CopyClass.CONTENT,CopyClass.CANONICAL,CopyClass.AUDIT,CopyClass.CONFIG,CopyClass.EXPORT}),
    'index':frozenset({CopyClass.INDEX}),'file':frozenset({CopyClass.CONTENT,CopyClass.CANONICAL,CopyClass.EXPORT}),
    'cache':frozenset({CopyClass.CACHE,CopyClass.EXPORT}),'checkpoint':frozenset({CopyClass.CHECKPOINT}),
    'ledger':frozenset({CopyClass.CANONICAL,CopyClass.AUDIT}),'backup':frozenset({CopyClass.BACKUP}),
    'pins':frozenset({CopyClass.CANONICAL}),'provider':frozenset({CopyClass.PROVIDER}),
}
COPY_CLOCK=frozenset({CopyClass.INDEX,CopyClass.CACHE,CopyClass.CHECKPOINT,CopyClass.BACKUP,CopyClass.PROVIDER,CopyClass.EXPORT})


def timestamp(value):return type(value) in (int,float) and math.isfinite(value) and 0<=value<=MAX_TIME
def seconds(value,*,zero=False):return type(value) is int and (0 if zero else 1)<=value<=MAX_SECONDS


@dataclass(frozen=True)
class CopyRule:
    category:Category
    store:str
    copy_class:CopyClass
    min_retain_seconds:int|str
    expire_after_seconds:int
    clock_basis:ClockBasis
    regions:tuple[str,...]
    provider_retention_seconds:int|str
    training_allowed:bool

    def validate(self):
        if (type(self.category) is not Category or type(self.store) is not str or self.store not in REQUIRED[self.category]
            or type(self.copy_class) is not CopyClass or self.copy_class not in ALLOWED_CLASSES[self.store]
            or type(self.clock_basis) is not ClockBasis or self.clock_basis is not (ClockBasis.COPY_CREATED if self.copy_class in COPY_CLOCK else ClockBasis.CREATED)
            or not seconds(self.expire_after_seconds) or self.training_allowed is not False):raise PrivacyFailure('invalid')
        if not (seconds(self.min_retain_seconds,zero=True) or type(self.min_retain_seconds) is str and self.min_retain_seconds=='not_applicable'):raise PrivacyFailure('invalid')
        if self.minimum()>self.expire_after_seconds:raise PrivacyFailure('invalid')
        if (type(self.regions) is not tuple or not 1<=len(self.regions)<=10 or any(type(v) is not str or not 1<=len(v)<=64 or not v.isascii() or any(not(c.isalnum() or c=='-') for c in v) for v in self.regions)
            or len(set(self.regions))!=len(self.regions)):raise PrivacyFailure('invalid')
        if self.store=='provider':
            if not seconds(self.provider_retention_seconds,zero=True) or not self.minimum()<=self.provider_retention_seconds<=self.expire_after_seconds:raise PrivacyFailure('invalid')
        elif type(self.provider_retention_seconds) is not str or self.provider_retention_seconds!='not_applicable':raise PrivacyFailure('invalid')

    def minimum(self):return 0 if self.min_retain_seconds=='not_applicable' else self.min_retain_seconds
    def maximum(self):return self.provider_retention_seconds if self.store=='provider' else self.expire_after_seconds
    def key(self):return self.category.value,self.store,self.copy_class.value
    def record(self):return dict(category=self.category.value,store=self.store,copy_class=self.copy_class.value,min_retain_seconds=self.min_retain_seconds,expire_after_seconds=self.expire_after_seconds,clock_basis=self.clock_basis.value,regions=self.regions,provider_retention_seconds=self.provider_retention_seconds,training_allowed=self.training_allowed)


@dataclass(frozen=True)
class EffectPolicy:
    schema_version:int
    tenant_id:str
    revision:int
    approved_by:str
    approved_at:float
    expires_at:float
    rules:tuple[CopyRule,...]

    def validate(self,now):
        identifier(self.tenant_id);identifier(self.approved_by)
        if (type(self.schema_version) is not int or self.schema_version!=2 or type(self.revision) is not int or not 1<=self.revision<10**18
            or not timestamp(now) or not timestamp(self.approved_at) or not timestamp(self.expires_at) or not self.approved_at<=now<self.expires_at
            or type(self.rules) is not tuple or not 1<=len(self.rules)<=128 or any(type(r) is not CopyRule for r in self.rules)):raise PrivacyFailure('policy_missing')
        for rule in self.rules:rule.validate()
        if len({r.key() for r in self.rules})!=len(self.rules):raise PrivacyFailure('invalid')
        if {(r.category,r.store) for r in self.rules}!={(c,s) for c,stores in REQUIRED.items() for s in stores}:raise PrivacyFailure('policy_missing')

    def fingerprint(self):
        return digest(dict(schema_version=self.schema_version,tenant_id=self.tenant_id,revision=self.revision,approved_by=self.approved_by,approved_at=self.approved_at,expires_at=self.expires_at,rules=[r.record() for r in sorted(self.rules,key=lambda r:r.key())]))


@dataclass(frozen=True)
class CopyFacts:
    tenant_id:str
    resource_id:str
    version:str
    content_digest:str
    category:Category
    store:str
    copy_class:CopyClass
    region:str
    created_at:float|None
    copy_created_at:float|None


@dataclass(frozen=True)
class Hold:
    tenant_id:str
    resource_id:str
    authority_id:str
    reason:str
    approved_at:float
    expires_at:float|None
    review_at:float|None
    released_at:float|None

    def validate(self,asset,now):
        identifier(self.authority_id)
        if self.tenant_id!=asset.tenant_id or self.resource_id!=asset.resource_id or self.reason not in {'owner_hold','pending_commit','receipt_pin','incomplete_inventory','provider_unknown','backup_unknown'}:raise PrivacyFailure('held')
        if not timestamp(self.approved_at) or self.approved_at>now:raise PrivacyFailure('held')
        if self.expires_at is None and self.review_at is None:raise PrivacyFailure('held')
        for value in (self.expires_at,self.review_at,self.released_at):
            if value is not None and (not timestamp(value) or value<self.approved_at):raise PrivacyFailure('held')
        if self.released_at is not None and self.released_at>now:raise PrivacyFailure('held')


@dataclass(frozen=True)
class EffectDecision:
    status:str
    reason:str
    minimum_at:float|None=None
    expires_at:float|None=None
    policy_hash:str|None=None


def decide(policy,asset,mode,now,holds=()):
    """An eligibility result is NOT authority to mutate; owning store rechecks."""
    if type(policy) is not EffectPolicy or type(asset) is not CopyFacts or type(mode) is not Mode:return EffectDecision('held','effect_policy_required')
    try:
        policy.validate(now);identifier(asset.tenant_id);identifier(asset.resource_id)
        if (asset.tenant_id!=policy.tenant_id or not isinstance(asset.version,str) or not 1<=len(asset.version)<=128
            or not isinstance(asset.content_digest,str) or len(asset.content_digest)!=64 or any(c not in '0123456789abcdef' for c in asset.content_digest)):
            return EffectDecision('held','asset_scope_invalid')
        rule=next((r for r in policy.rules if r.category is asset.category and r.store==asset.store and r.copy_class is asset.copy_class),None)
        if rule is None:return EffectDecision('held','copy_class_unconfigured')
        if asset.region not in rule.regions:return EffectDecision('held','residency_unverified')
        created=asset.copy_created_at if rule.clock_basis is ClockBasis.COPY_CREATED else asset.created_at
        if not timestamp(created) or created>now:return EffectDecision('held','copy_clock_unverified')
        minimum=created+rule.minimum();expiry=created+rule.maximum();fingerprint=policy.fingerprint()
        if not timestamp(minimum) or not timestamp(expiry):return EffectDecision('held','clock_overflow')
        if type(holds) is not tuple:return EffectDecision('held','hold_inventory_invalid')
        for hold in holds:
            if type(hold) is not Hold:return EffectDecision('held','hold_inventory_invalid')
            hold.validate(asset,now)
            if hold.released_at is None:
                overdue=hold.review_at is not None and now>=hold.review_at or hold.expires_at is not None and now>=hold.expires_at
                return EffectDecision('overdue_held' if now>=expiry else 'held','hold_review_or_release_required' if overdue else 'hold_active',minimum,expiry,fingerprint)
        if now<minimum:return EffectDecision('retained','minimum_retention',minimum,expiry,fingerprint)
        if mode is Mode.SCHEDULED_EXPIRY and now<expiry:return EffectDecision('not_due','maximum_not_reached',minimum,expiry,fingerprint)
        return EffectDecision('ready','minimum_satisfied' if mode is Mode.REQUEST_DELETE else 'maximum_reached',minimum,expiry,fingerprint)
    except PrivacyFailure:return EffectDecision('held','policy_or_hold_unverified')


@dataclass(frozen=True)
class ExportPlan:
    status:str
    reason:str
    download_expires_at:float|None=None
    storage_expires_at:float|None=None
    minimum_at:float|None=None
    policy_hash:str|None=None


def plan_export(policy,copy,now):
    """Only bounds a prospective private artifact; never reads source content."""
    if type(copy) is not CopyFacts or copy.copy_class is not CopyClass.EXPORT:return ExportPlan('held','export_class_required')
    decision=decide(policy,copy,Mode.REQUEST_DELETE,now)
    if decision.status not in {'ready','retained'}:return ExportPlan('held',decision.reason)
    if now>=decision.expires_at:return ExportPlan('held','artifact_already_expired')
    return ExportPlan('ready','explicit_artifact_policy',min(decision.expires_at,policy.expires_at),decision.expires_at,decision.minimum_at,decision.policy_hash)
