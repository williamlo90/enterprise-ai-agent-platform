"""Application-local readiness state; no global mutable process state."""

from dataclasses import dataclass
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase


@dataclass
class RuntimeReadiness:
    ready: bool = False
    database: RuntimeDatabase | None = None

    def is_ready(self) -> bool:
        return self.ready

    async def check(self) -> bool:
        return self.ready and (self.database is None or await self.database.ping())
