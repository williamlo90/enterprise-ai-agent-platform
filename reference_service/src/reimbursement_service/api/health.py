"""Only foundation endpoints; no identity or business API is exposed."""
from fastapi import APIRouter, Request
from reimbursement_service.api.errors import error_response
from reimbursement_service.modules.system.ports import HealthPort


def router(health: HealthPort):
    routes = APIRouter()

    @routes.get("/health/live")
    async def live():
        return {"status": "alive"}

    @routes.get("/health/ready")
    async def ready(request: Request):
        if not await health.ready():
            return error_response(503, request.state.request_id)
        return {"status": "ready"}

    return routes
