"""Health dependency contract."""
from typing import Protocol


class ReadinessPort(Protocol):
    async def ping(self) -> bool: ...


class HealthPort(Protocol):
    async def ready(self) -> bool: ...
