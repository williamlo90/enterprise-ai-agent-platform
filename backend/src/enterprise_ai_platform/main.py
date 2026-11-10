"""API bootstrap and HTTP boundary; business routes follow later tasks."""

import argparse
import logging
import sys
from pathlib import Path
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from enterprise_ai_platform import __version__
from enterprise_ai_platform.api.boundary import RequestBoundary
from enterprise_ai_platform.api.browser_context import BrowserContextBoundary
from enterprise_ai_platform.api.errors import install_error_handlers
from enterprise_ai_platform.api.conversations import router as conversation_router
from enterprise_ai_platform.api.knowledge import router as knowledge_router
from enterprise_ai_platform.api.organization import router as organization_router
from enterprise_ai_platform.api.files import router as files_router
from enterprise_ai_platform.api.health import router as health_router
from enterprise_ai_platform.api.identity import router as identity_router
from enterprise_ai_platform.api.delegation import router as delegation_router
from enterprise_ai_platform.api.policies import router as policy_router,internal_router as policy_internal_router
from enterprise_ai_platform.api.trips import router as trips_router
from enterprise_ai_platform.api.receipts import router as receipt_router
from enterprise_ai_platform.api.drafts import router as draft_router
from enterprise_ai_platform.api.workspace import router as workspace_router
from enterprise_ai_platform.api.administration import router as administration_router
from enterprise_ai_platform.api.privacy import router as privacy_router
from enterprise_ai_platform.api.actions import router as action_router
from enterprise_ai_platform.api.receipt_boundary import ReceiptResponseBoundary
from enterprise_ai_platform.api.identity_demo import router as identity_demo_router
from enterprise_ai_platform.api.schemas import ErrorResponse
from enterprise_ai_platform.config import ConfigurationError, Settings, load_settings
from enterprise_ai_platform.composition import ApplicationServices, build_resources, build_services
from enterprise_ai_platform.infrastructure.database.connection import DatabaseConfigurationError


def create_app(settings: Settings, *, services: ApplicationServices | None = None) -> FastAPI:
    return build_app(settings, services=services, internal=False)


def build_app(settings: Settings, *, services: ApplicationServices | None = None, internal=False) -> FastAPI:
    resources = build_resources(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with resources.lifecycle():
            yield

    development = settings.env == "development" and not internal
    app = FastAPI(
        title="Enterprise AI Agent Platform API",
        version=__version__,
        debug=False,
        docs_url="/docs" if development else None,
        redoc_url=None,
        openapi_url="/openapi.json" if development else None,
        lifespan=lifespan,
        responses={code: {"model": ErrorResponse} for code in (400, 404, 405, 408, 413, 422, 500)},
    )
    app.state.settings = settings
    app.state.resources = resources
    app.state.services = build_services(resources) if services is None else services
    install_error_handlers(app)
    app.include_router(health_router)
    if not internal and app.state.services.conversations is not None:
        app.include_router(conversation_router)
    if not internal and app.state.services.knowledge is not None:
        app.include_router(knowledge_router)
    if not internal and app.state.services.organization is not None:
        app.include_router(organization_router)
    if not internal and settings.files_upload_enabled and app.state.services.files is not None:
        app.include_router(files_router)
    if not internal and app.state.services.administration is not None:app.include_router(administration_router)
    if not internal and app.state.services.privacy_policy is not None:app.include_router(privacy_router)
    if not internal and app.state.services.workspace is not None:app.include_router(workspace_router)
    if not internal and app.state.services.drafts is not None:
        app.include_router(draft_router)
    if not internal and app.state.services.actions is not None:
        app.include_router(action_router)
    if internal and app.state.services.delegation is not None:
        app.include_router(delegation_router)
    if internal and app.state.services.receipts is not None:
        app.include_router(receipt_router)
    if app.state.services.policies is not None:
        app.include_router(policy_internal_router if internal else policy_router)
    if not internal and app.state.services.trips is not None:
        app.include_router(trips_router)
    if not internal and app.state.services.identity is not None:
        app.include_router(identity_router)
        if settings.auth_demo_ui and settings.env == "development":
            app.include_router(identity_demo_router)
    app.add_middleware(BrowserContextBoundary, enabled=settings.browser_context_enabled and not internal)
    # The receipt deadline guard must sit OUTSIDE the browser binding guard so
    # its post-header check follows the actual downstream header await.
    app.add_middleware(ReceiptResponseBoundary)
    app.add_middleware(
        RequestBoundary, max_body_bytes=settings.max_request_body_bytes,
        body_timeout=settings.request_body_timeout_seconds,
        file_body_bytes=10485760 if not internal and app.state.services.files is not None else 0,
    )
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Enterprise AI Platform API.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--env-file", type=Path, help="Load an explicit UTF-8 dotenv file.")
    source.add_argument("--no-env-file", action="store_true", help="Use process environment only.")
    parser.add_argument("--check-config", action="store_true", help="Validate and exit without a server.")
    args = parser.parse_args()
    path = None if args.no_env_file else args.env_file or Path(".env")
    try:
        settings = load_settings(path, require_file=args.env_file is not None)
        app = create_app(settings)
    except (ConfigurationError, DatabaseConfigurationError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    if args.check_config:
        print("Configuration valid.")
        return
    # Raw paths/query strings can contain private data, even on rejected routes.
    http_logger = logging.getLogger("enterprise_ai_platform.http")
    if not http_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        http_logger.addHandler(handler)
    http_logger.setLevel(logging.INFO)
    http_logger.propagate = False
    uvicorn.run(
        app, host=settings.host, port=settings.port,
        access_log=False, ws="none", proxy_headers=False,
        loop="enterprise_ai_platform.composition:database_loop_factory" if settings.database_enabled else "auto",
    )


if __name__ == "__main__":
    main()
