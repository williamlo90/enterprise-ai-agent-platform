"""Composition root; resources are owned by one application lifecycle."""

from contextlib import asynccontextmanager
from dataclasses import dataclass

from enterprise_ai_platform.infrastructure.runtime import RuntimeReadiness
from enterprise_ai_platform.modules.system.ports import HealthServicePort
from enterprise_ai_platform.modules.system.service import HealthService


@dataclass(frozen=True)
class ApplicationServices:
    health: HealthServicePort


@dataclass(frozen=True)
class ApplicationResources:
    readiness: RuntimeReadiness

    @asynccontextmanager
    async def lifecycle(self):
        self.readiness.ready = True
        try:
            yield
        finally:
            self.readiness.ready = False


def build_resources() -> ApplicationResources:
    return ApplicationResources(readiness=RuntimeReadiness())


def build_services(resources: ApplicationResources) -> ApplicationServices:
    return ApplicationServices(health=HealthService(readiness=resources.readiness))
