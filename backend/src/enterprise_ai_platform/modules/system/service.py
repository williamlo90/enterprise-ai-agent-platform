"""Health behavior consumes a readiness port, not an HTTP request or DB client."""

from dataclasses import dataclass

from enterprise_ai_platform.modules.system.domain import ProbeResult
from enterprise_ai_platform.modules.system.ports import BootstrapReadiness


@dataclass(frozen=True)
class HealthService:
    readiness: BootstrapReadiness

    def live(self) -> ProbeResult:
        return ProbeResult(status="ok", scope="process")

    async def ready(self) -> ProbeResult:
        return ProbeResult(
            status="ready" if await self.readiness.check() else "unavailable",
            scope="bootstrap",
        )
