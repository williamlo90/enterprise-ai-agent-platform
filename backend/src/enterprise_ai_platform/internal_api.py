"""Separate service-only ASGI factory. Deploy behind private ingress, never browser hosting."""

import argparse
from pathlib import Path
import uvicorn
from enterprise_ai_platform.config import load_settings,ConfigurationError
from enterprise_ai_platform.main import build_app


def create_internal_app(settings, *, services=None):
    return build_app(settings,services=services,internal=True)


def create_internal_application():
    settings=load_settings(None)
    if not settings.internal_auth_enabled:
        raise ConfigurationError("Internal verification must be explicitly enabled.")
    return create_internal_app(settings)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",type=Path)
    args=parser.parse_args()
    try:
        settings=load_settings(args.env_file,require_file=args.env_file is not None)
        if not settings.internal_auth_enabled:
            raise ConfigurationError("Internal verification must be explicitly enabled.")
        app=create_internal_app(settings)
    except ConfigurationError as error:
        print(str(error))
        raise SystemExit(2) from None
    # A separate private listener/ingress is an operator deployment requirement.
    uvicorn.run(app,host=settings.host,port=settings.port,access_log=False,proxy_headers=False,
                loop="enterprise_ai_platform.composition:database_loop_factory")


if __name__=="__main__":main()
