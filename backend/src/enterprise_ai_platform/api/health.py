"""Process and bootstrap-readiness probes; future dependencies extend readiness."""

from fastapi import APIRouter, Request

from enterprise_ai_platform.api.errors import error_response
from enterprise_ai_platform.api.schemas import ErrorResponse, HealthResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return HealthResponse(status="ok", scope="process")


@router.get("/ready", response_model=HealthResponse, responses={503: {"model": ErrorResponse}})
async def ready(request: Request):
    if not request.app.state.ready:
        return error_response(503, request.state.request_id)
    return HealthResponse(status="ready", scope="bootstrap")
