"""Foundation facts. Context must be supplied by trusted application code."""
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ActorContext:
    tenant_id: UUID
    actor_id: UUID

    def __post_init__(self):
        if any(type(value) is not UUID or value.int == 0 for value in (self.tenant_id, self.actor_id)):
            raise ValueError("Invalid internal actor context.")


class DatabaseUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Service database unavailable.")
