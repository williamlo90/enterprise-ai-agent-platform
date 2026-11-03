"""Identifiers supplied by trusted backend identity/policy resolution."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, repr=False)
class TenantActorContext:
    tenant_id: UUID
    actor_id: UUID

    def __post_init__(self):
        if not isinstance(self.tenant_id, UUID) or not isinstance(self.actor_id, UUID):
            raise ValueError("Database context requires UUID identifiers.")

    def __repr__(self):
        return "TenantActorContext(<redacted>)"
