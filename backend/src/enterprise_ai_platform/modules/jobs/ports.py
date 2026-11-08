"""Job persistence is an explicit port, with safe failures."""
from typing import Protocol
from enterprise_ai_platform.modules.jobs.domain import Lease


class JobFailure(RuntimeError):
    def __init__(self, status='unavailable'):
        self.status = status
        super().__init__('Job operation rejected.')


class JobRepository(Protocol):
    async def call(self, operation: str, payload: dict) -> dict: ...
