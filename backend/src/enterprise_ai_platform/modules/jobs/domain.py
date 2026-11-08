"""Bounded, reference-only job requests and lease handles."""

from dataclasses import dataclass
from uuid import UUID

JOB_TYPES = frozenset({'knowledge.ingest', 'agent.resume', 'action.dispatch', 'action.reconcile', 'file.inspect'})
ERROR_CODES = frozenset({'transient', 'rate_limited', 'invalid', 'forbidden', 'unknown_commit'})


def identifier(value):
    if not isinstance(value, UUID):
        raise ValueError('A UUID is required.')


def positive(value, maximum=10**18-1):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError('Invalid positive integer.')


@dataclass(frozen=True)
class JobRequest:
    id: UUID
    job_type: str
    business_key: UUID
    target_id: UUID
    expected_version: int
    actor_id: UUID
    membership_id: UUID
    session_id: UUID
    access_revision: int
    retry_limit: int = 3

    def __post_init__(self):
        for value in (self.id, self.business_key, self.target_id, self.actor_id, self.membership_id, self.session_id):
            identifier(value)
        if self.job_type not in JOB_TYPES:
            raise ValueError('Unknown job type.')
        positive(self.expected_version)
        positive(self.access_revision)
        positive(self.retry_limit, 5)

    def payload(self):
        return {name: str(value) if isinstance(value, UUID) else value for name, value in vars(self).items()}


@dataclass(frozen=True)
class Lease:
    id: UUID
    tenant_id: UUID
    generation: int

    def __post_init__(self):
        identifier(self.id)
        identifier(self.tenant_id)
        positive(self.generation)
