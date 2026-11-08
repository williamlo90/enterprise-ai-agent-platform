"""Deny-by-default policy over trusted, current repository facts; never browser claims."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class Action(StrEnum):
    READ = "read"
    WRITE = "write"
    MANAGE_ACL = "manage_acl"
    PUBLISH = "publish"
    DECIDE = "decide"
    CONFIGURE = "configure"
    OPERATE = "operate"


KINDS = frozenset({"document", "conversation", "draft", "trip", "claim", "receipt", "action",
                   "policy_package", "tenant_config", "membership", "job_metadata"})
CONFIG_FIELDS = frozenset({"assistant_name", "locales", "help_contact", "instruction_text",
                           "connector_ref", "model_policy_ref"})
ROLES = frozenset({"employee", "knowledge_manager", "tenant_admin", "process_owner", "approver", "operator_scoped"})


@dataclass(frozen=True)
class RoleGrant:
    role: str
    scope_kind: str
    scope_ref: str

    def valid(self, tenant: UUID) -> bool:
        if self.role not in ROLES:
            return False
        if self.role in {"employee", "tenant_admin"}:
            return (self.scope_kind, self.scope_ref) == ("tenant", str(tenant))
        if self.role == "knowledge_manager":
            if (self.scope_kind, self.scope_ref) == ("tenant", str(tenant)):
                return True
            try:
                return self.scope_kind == "knowledge_collection" and str(UUID(self.scope_ref)) == self.scope_ref
            except (ValueError, TypeError, AttributeError):
                return False
        if self.role in {"process_owner", "approver"}:
            return (self.scope_kind, self.scope_ref) == ("process", "reimbursement")
        return self.scope_kind == "operations" and self.scope_ref in {"health", "provisioning", "job_metadata", "config"}


@dataclass(frozen=True, repr=False)
class Subject:
    tenant_id: UUID
    principal_id: UUID
    membership_id: UUID
    access_revision: int
    grants: tuple[RoleGrant, ...]

    def __post_init__(self):
        if (not all(isinstance(v, UUID) for v in (self.tenant_id, self.principal_id, self.membership_id))
                or type(self.access_revision) is not int or self.access_revision < 1
                or type(self.grants) is not tuple or not all(isinstance(g, RoleGrant) for g in self.grants)):
            raise ValueError("Invalid authorization subject.")

    def has(self, role, kind, ref):
        return any(g.valid(self.tenant_id) and (g.role, g.scope_kind, g.scope_ref) == (role, kind, ref) for g in self.grants)


@dataclass(frozen=True)
class ResourceRef:
    tenant_id: UUID
    kind: str
    object_id: UUID

    def __post_init__(self):
        if self.kind not in KINDS or not isinstance(self.tenant_id, UUID) or not isinstance(self.object_id, UUID):
            raise ValueError("Invalid resource reference.")


@dataclass(frozen=True)
class ACLGrant:
    permission: str
    membership_id: UUID | None = None
    role: RoleGrant | None = None

    def matches(self, subject, permission):
        return (self.permission == permission and ((self.membership_id == subject.membership_id and self.role is None)
            or (self.membership_id is None and self.role is not None and self.role in subject.grants and self.role.valid(subject.tenant_id))))


@dataclass(frozen=True, repr=False)
class Resource:
    ref: ResourceRef
    version: int
    access_revision: int
    owner_id: UUID | None = None
    collection_id: UUID | None = None
    published: bool = False
    withdrawn: bool = False
    acl: tuple[ACLGrant, ...] = ()
    process: str = "reimbursement"
    relation_verified: bool = False
    active_reviewer_id: UUID | None = None
    active_stage: bool = False
    receipt_revision_bound: bool = False

    def __post_init__(self):
        if (not isinstance(self.ref, ResourceRef) or any(type(v) is not int or v < 1 for v in (self.version, self.access_revision))
                or any(v is not None and not isinstance(v, UUID) for v in (self.owner_id, self.collection_id, self.active_reviewer_id))
                or any(type(v) is not bool for v in (self.published, self.withdrawn, self.relation_verified, self.active_stage, self.receipt_revision_bound))
                or type(self.acl) is not tuple or not all(isinstance(g, ACLGrant) for g in self.acl)):
            raise ValueError("Invalid resource authorization facts.")


class AccessPermit:
    """Backend-only receipt of a feature repository's atomic versioned write."""

    def __init__(self, resource, action, apply_write=None):
        self.resource, self.action, self._apply_write = resource, action, apply_write
        self.expected_after = None

    async def apply(self, command):
        if self.action == Action.READ or self.expected_after is not None or self._apply_write is None:
            raise ValueError("Invalid versioned write request.")
        # The bound adapter uses this access unit's transaction and expected-version CAS.
        stored_resource = await self._apply_write(command)
        if (self.expected_after is not None
                or not isinstance(stored_resource, Resource) or stored_resource.ref != self.resource.ref
                or stored_resource.version != self.resource.version + 1):
            raise ValueError("Invalid versioned write receipt.")
        self.expected_after = stored_resource
        return stored_resource


def evaluate(subject: Subject, resource: Resource | None, action: Action, *, changed_fields=frozenset()) -> int:
    """0 allowed; hidden/foreign object 404; forbidden action on a visible object 403."""
    if resource is None or resource.ref.tenant_id != subject.tenant_id or resource.withdrawn:
        return 404
    if not isinstance(action, Action):
        return 403
    tenant = str(subject.tenant_id)
    employee = subject.has("employee", "tenant", tenant)
    admin = subject.has("tenant_admin", "tenant", tenant)
    owner = resource.owner_id == subject.principal_id
    kind = resource.ref.kind
    allowed = set()
    if kind in {"conversation", "draft", "action"} and employee and owner:
        allowed = {Action.READ, Action.WRITE}
    elif kind in {"trip", "claim", "receipt"} and resource.relation_verified:
        reviewer = (kind in {"claim", "receipt"} and resource.active_stage and resource.active_reviewer_id == subject.principal_id
                    and not owner and subject.has("approver", "process", resource.process))
        if kind == "receipt" and not resource.receipt_revision_bound:
            return 404
        if employee and owner:
            allowed.add(Action.READ)
        if reviewer:
            allowed.add(Action.READ)
            if kind == "claim":
                allowed.add(Action.DECIDE)
    elif kind == "document":
        manager = (subject.has("knowledge_manager", "tenant", tenant) or (resource.collection_id is not None
            and subject.has("knowledge_manager", "knowledge_collection", str(resource.collection_id))))
        if manager or any(g.matches(subject, "manage") for g in resource.acl):
            allowed |= {Action.READ, Action.WRITE}
        if manager:
            allowed.add(Action.MANAGE_ACL)
        if manager or any(g.matches(subject, "publish") for g in resource.acl):
            allowed |= {Action.READ, Action.PUBLISH}
        if resource.published and any(g.matches(subject, "read") for g in resource.acl):
            allowed.add(Action.READ)
    elif kind == "policy_package" and subject.has("process_owner", "process", resource.process):
        allowed = {Action.READ, Action.WRITE, Action.PUBLISH}
    elif kind in {"tenant_config", "membership"} and admin:
        allowed = {Action.READ}
        if kind == "tenant_config" and changed_fields and changed_fields <= CONFIG_FIELDS:
            allowed.add(Action.CONFIGURE)
    elif kind == "job_metadata" and (admin or subject.has("operator_scoped", "operations", "job_metadata")):
        allowed = {Action.READ, Action.OPERATE}
    # Explicit prohibitions apply after combining roles/ACLs.
    if action == Action.DECIDE and owner:
        return 403 if Action.READ in allowed else 404
    if Action.READ not in allowed:
        return 404
    return 0 if action in allowed else 403
