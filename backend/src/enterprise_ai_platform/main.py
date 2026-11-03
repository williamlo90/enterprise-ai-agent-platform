"""BE-01 API bootstrap; feature routes follow subsequent task-plan entries."""

import argparse
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from enterprise_ai_platform import __version__
from enterprise_ai_platform.config import ConfigurationError, Settings, load_settings


def create_app(settings: Settings) -> FastAPI:
    development = settings.env == "development"
    app = FastAPI(
        title="Enterprise AI Agent Platform API",
        version=__version__,
        debug=False,
        docs_url="/docs" if development else None,
        redoc_url=None,
        openapi_url="/openapi.json" if development else None,
    )
    app.state.settings = settings
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
    uvicorn.run(
        create_app(settings), host=settings.host, port=settings.port,
        access_log=False, ws="none",
    )


if __name__ == "__main__":
    main()
