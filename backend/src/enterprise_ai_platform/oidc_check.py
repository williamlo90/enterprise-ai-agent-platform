"""Cognito preflight CLI; never launches a login or creates an application session."""

import argparse
import asyncio
import json
from pathlib import Path

from enterprise_ai_platform.config import load_oidc_probe_settings
from enterprise_ai_platform.integrations.identity.cognito_probe import CognitoProbe
from enterprise_ai_platform.modules.identity.domain import CognitoProvider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--live", action="store_true", help="Fetch configured provider metadata and keys.")
    parser.add_argument("--check-client", action="store_true", help="Send a synthetic invalid code to the configured token endpoint.")
    args = parser.parse_args()
    if args.check_client and not args.live:
        parser.error("--check-client requires --live")
    try:
        settings = load_oidc_probe_settings(args.env_file)
        values = settings.model_dump()
        values["client_secret"] = settings.client_secret.get_secret_value()
        provider = CognitoProvider(**values)
        if not args.live:
            print("OIDC probe configuration valid. No provider request made.")
            return
        checks = asyncio.run(CognitoProbe(provider).preflight(check_client=args.check_client))
        print(json.dumps({"checks": checks, "actual_login_callback": "not_tested"}, sort_keys=True))
    except Exception:
        print("OIDC preflight failed. Check local configuration and provider connectivity.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
