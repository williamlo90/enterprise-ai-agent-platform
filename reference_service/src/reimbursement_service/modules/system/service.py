"""Liveness is transport-owned; readiness requires a live guarded database."""
from reimbursement_service.modules.system.ports import ReadinessPort


class HealthService:
    def __init__(self, database: ReadinessPort):
        self.database = database

    async def ready(self) -> bool:
        return await self.database.ping()
