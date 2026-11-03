"""Composition root; resources are owned by one application lifecycle."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
import asyncio
import sys

from enterprise_ai_platform.config import ConfigurationError, Settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase, parse_target
from enterprise_ai_platform.infrastructure.runtime import RuntimeReadiness
from enterprise_ai_platform.modules.system.ports import HealthServicePort
from enterprise_ai_platform.modules.system.service import HealthService


@dataclass(frozen=True)
class ApplicationServices:
    health: HealthServicePort


@dataclass(frozen=True)
class ApplicationResources:
    readiness: RuntimeReadiness
    database: RuntimeDatabase | None = None

    @asynccontextmanager
    async def lifecycle(self):
        try:
            if self.database is not None:
                await self.database.open()
            self.readiness.ready = True
            yield
        finally:
            self.readiness.ready = False
            if self.database is not None:
                await self.database.close()


def build_resources(settings: Settings | None = None) -> ApplicationResources:
    database = None
    if settings is not None and settings.database_enabled:
        if settings.database_url is None:
            raise ConfigurationError("Missing or invalid configuration: DATABASE_URL")
        database = RuntimeDatabase(parse_target(settings.database_url.get_secret_value()))
    return ApplicationResources(readiness=RuntimeReadiness(database=database), database=database)


def database_loop_factory():
    # psycopg async does not support the default Windows Proactor loop.
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def build_services(resources: ApplicationResources) -> ApplicationServices:
    return ApplicationServices(health=HealthService(readiness=resources.readiness))
