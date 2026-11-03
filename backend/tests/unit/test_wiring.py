import asyncio
from dataclasses import FrozenInstanceError

import pytest
from fastapi.testclient import TestClient

from enterprise_ai_platform.api.dependencies import get_health_service
from enterprise_ai_platform.composition import ApplicationServices, build_resources, build_services
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.system.domain import ProbeResult


class UnavailableHealth:
    def __init__(self):
        self.calls = []

    def live(self):
        self.calls.append("live")
        return ProbeResult("ok", "process")

    def ready(self):
        self.calls.append("ready")
        return ProbeResult("unavailable", "bootstrap")


def settings():
    return Settings(env="test", host="127.0.0.1", port=8000)


def test_factory_override_reaches_real_routes_without_affecting_other_app():
    fake = UnavailableHealth()
    first = create_app(settings(), services=ApplicationServices(health=fake))
    second = create_app(settings())
    assert first.state.resources is not second.state.resources
    assert first.state.services is not second.state.services
    with TestClient(first) as a, TestClient(second) as b:
        assert a.get("/health/live").json() == {"status": "ok", "scope": "process"}
        response = a.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
        assert b.get("/health/ready").status_code == 200
    assert fake.calls == ["live", "ready"]
    assert not first.state.resources.readiness.is_ready()
    assert not second.state.resources.readiness.is_ready()


def test_fastapi_dependency_override_is_local_to_one_instance():
    first, second = create_app(settings()), create_app(settings())
    fake = UnavailableHealth()
    first.dependency_overrides[get_health_service] = lambda: fake
    with TestClient(first) as a, TestClient(second) as b:
        assert a.get("/health/ready").status_code == 503
        assert b.get("/health/ready").status_code == 200
        first.dependency_overrides.clear()
        assert a.get("/health/ready").status_code == 200


def test_service_consumes_readiness_port_and_has_no_http_dependency():
    resources = build_resources()
    service = build_services(resources).health
    assert service.live() == ProbeResult("ok", "process")
    assert service.ready() == ProbeResult("unavailable", "bootstrap")

    async def check():
        async with resources.lifecycle():
            assert service.ready() == ProbeResult("ready", "bootstrap")
            raise RuntimeError("synthetic lifecycle failure")

    with pytest.raises(RuntimeError, match="synthetic lifecycle failure"):
        asyncio.run(check())
    assert service.ready() == ProbeResult("unavailable", "bootstrap")


def test_services_container_is_immutable_and_resources_are_not_global():
    first, second = build_resources(), build_resources()
    services = build_services(first)
    first.readiness.ready = True
    assert second.readiness.is_ready() is False
    with pytest.raises(FrozenInstanceError):
        services.health = UnavailableHealth()
