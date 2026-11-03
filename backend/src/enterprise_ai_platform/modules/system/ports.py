"""Small contracts used by the current health path."""

from typing import Protocol

from enterprise_ai_platform.modules.system.domain import ProbeResult


class BootstrapReadiness(Protocol):
    async def check(self) -> bool: ...


class HealthServicePort(Protocol):
    def live(self) -> ProbeResult: ...

    async def ready(self) -> ProbeResult: ...
