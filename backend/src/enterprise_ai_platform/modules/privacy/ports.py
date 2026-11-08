from typing import Protocol
from enterprise_ai_platform.modules.privacy.domain import Policy,PolicyDraft,Grant,Inventory,Asset,Category,PrivacyFailure


class PrivacyDownloadExpired(RuntimeError):
    status=410

    def __init__(self):super().__init__('Private download expired.')


class PrivacyAuthority(Protocol):
    async def current(self,credential,tenant_id,subject_id,operation,category:Category)->Grant: ...
    # Atomically recheck current authority and fence growth; operation_id is an
    # idempotency key. A prior Grant cannot install a hold after revocation.
    async def hold(self,grant:Grant,operation_id:str,*,policy_digest:str)->None: ...


class PolicyRegistry(Protocol):
    # Implementations establish approver authority; a request JSON is not a policy.
    async def current(self,tenant_id:str)->Policy|None: ...


class PolicyPublisher(Protocol):
    # Durably establish owner approval at the authoritative producer for this
    # exact candidate. This returns a committed decision, not a reusable allow
    # token; approved_by/approved_at never come from untrusted frontend input.
    async def approve(self,credential,candidate:PolicyDraft)->Policy: ...


class DenyPolicyPublisher:
    async def approve(self,*args):raise PrivacyFailure('denied')


class PrivacyRepository(Protocol):
    async def create(self,tenant_id,operation_id,request_digest,body)->dict: ...
    async def get(self,tenant_id,operation_id)->dict|None: ...
    async def save(self,tenant_id,operation_id,expected_revision,body)->dict: ...


class PrivacyStores(Protocol):
    async def inventory(self,grant:Grant,category:Category)->Inventory: ...
    async def current(self,grant:Grant,asset:Asset,*,policy_digest:str)->bool: ...
    # Reconcile: applied/in_progress/unknown/sealed_not_applied. The last outcome
    # requires a durable seal that forbids every late writer for this effect key;
    # lookup404/absence is unknown. The kernel NEVER rearms an old effect key.
    async def reconcile(self,grant:Grant,asset:Asset,operation,effect_key,*,policy_digest:str)->str: ...
    # Owning adapters must atomically recheck current authority/tenant/subject,
    # version, retention and policy before their effect; Grant is not a bearer token.
    # No generic storage adapter is enabled by the kernel or its fixture coverage.
    async def apply(self,grant:Grant,asset:Asset,operation,effect_key,*,policy_digest:str)->str: ...


class PrivacyJournal(Protocol):
    async def intent(self,tenant_id,operation_id,effect_key,metadata)->None: ...
    async def result(self,tenant_id,operation_id,effect_key,outcome)->None: ...


class DenyPrivacyAuthority:
    async def current(self,*args):raise PrivacyFailure('denied')
    async def hold(self,*args):raise PrivacyFailure('denied')

# Public exports of versioned domain contracts and strict transport codec.
from enterprise_ai_platform.modules.privacy.domain.effects import CopyClass,ClockBasis,CopyRule,EffectPolicy
from enterprise_ai_platform.modules.privacy.domain.codec import copy_rules_from_json,effect_policy_from_json
