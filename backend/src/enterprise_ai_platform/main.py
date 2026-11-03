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
from enterprise_ai_platform.api.errors import install_error_handlers
from enterprise_ai_platform.api.health import router as health_router
from enterprise_ai_platform.api.schemas import ErrorResponse
from enterprise_ai_platform.config import ConfigurationError, Settings, load_settings


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False

    development = settings.env == "development"
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
    app.state.ready = False
    install_error_handlers(app)
    app.include_router(health_router)
    app.add_middleware(
        RequestBoundary, max_body_bytes=settings.max_request_body_bytes,
        body_timeout=settings.request_body_timeout_seconds,
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
    except ConfigurationError as exc:
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
        create_app(settings), host=settings.host, port=settings.port,
        access_log=False, ws="none",
    )


if __name__ == "__main__":
    main()
