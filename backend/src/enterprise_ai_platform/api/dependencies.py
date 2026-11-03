"""Request-scoped access to application service ports."""

from typing import cast

from fastapi import Request

from enterprise_ai_platform.modules.system.ports import HealthServicePort


def get_health_service(request: Request) -> HealthServicePort:
    return cast(HealthServicePort, request.app.state.services.health)
