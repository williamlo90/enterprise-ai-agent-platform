"""Process and bootstrap-readiness probes; future dependencies extend readiness."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from enterprise_ai_platform.api.errors import error_response
from enterprise_ai_platform.api.dependencies import get_health_service
from enterprise_ai_platform.api.schemas import ErrorResponse, HealthResponse
from enterprise_ai_platform.modules.system.ports import HealthServicePort

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthResponse)
async def live(service: Annotated[HealthServicePort, Depends(get_health_service)]) -> HealthResponse:
    result = service.live()
    return HealthResponse(status=result.status, scope=result.scope)


@router.get("/ready", response_model=HealthResponse, responses={503: {"model": ErrorResponse}})
async def ready(request: Request, service: Annotated[HealthServicePort, Depends(get_health_service)]):
    result = await service.ready()
    if result.status != "ready":
        return error_response(503, request.state.request_id)
    return HealthResponse(status=result.status, scope=result.scope)
