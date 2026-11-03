"""Explicit runtime-role provisioning and live isolation checks."""

import argparse
import asyncio
from hashlib import sha256
import os
from pathlib import Path
import sys
from urllib.parse import quote, urlsplit, urlunsplit

from dotenv import set_key

from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import ConfigurationError, DatabaseProbeSettings, load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase, parse_target, verify_same_database
from enterprise_ai_platform.infrastructure.database.roles import provision_role
from enterprise_ai_platform.infrastructure.database.security_probe import SecurityVerificationFailure, verify_security


async def check_runtime(target):
    database = RuntimeDatabase(target)
    try:
        await database.open()
        if not await database.ping():
            raise RuntimeError("Runtime database unavailable.")
    finally:
        await database.close()


def runtime_url(original: str, user: str, password: str) -> str:
    parts = urlsplit(original)
    # parse_target has already rejected multihost/IPv6/override parameters.
    host = parts.hostname
    authority = quote(user, safe="") + ":" + quote(password, safe="") + "@" + host
    if parts.port is not None:
        authority += ":" + str(parts.port)
    return urlunsplit((parts.scheme, authority, parts.path, parts.query, ""))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "provision", "check"))
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--env-file", type=Path)
    source.add_argument("--no-env-file", action="store_true")
    parser.add_argument("--rotate", action="store_true", help="Explicit recovery: rotate only a validated managed runtime login.")
    args = parser.parse_args()
    path = None if args.no_env_file else args.env_file or Path(".env")
    try:
        if args.rotate and args.command != "provision":
            raise ConfigurationError("Rotation is only valid with provisioning.")
        if args.command == "provision" and (path is None or not path.is_file() or "DATABASE_URL" in os.environ):
            raise ConfigurationError("Provisioning requires a local file without a DATABASE_URL process override.")
        settings = load_settings(path, require_file=args.env_file is not None, settings_type=DatabaseProbeSettings)
        original = settings.database_url.get_secret_value()
        base = parse_target(original)
        direct = parse_target(settings.database_direct_url.get_secret_value(), direct=True)
        verify_same_database(base, direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            if args.command == "verify":
                count = runner.run(verify_security(direct, base))
                print(f"Runtime isolation verification passed: {count} checks; disposable roles and schema removed.")
            elif args.command == "check":
                runner.run(check_runtime(base))
                print("Runtime login and least-privilege checks passed.")
            else:
                before = sha256(path.read_bytes()).digest()
                target = runner.run(provision_role(direct, base, rotate=args.rotate))
                if sha256(path.read_bytes()).digest() != before:
                    raise ConfigurationError("Local configuration changed during provisioning; retry locally.")
                value = runtime_url(original, target.user, target.password)
                if value == original:
                    print("Managed runtime login is already valid. Local configuration unchanged.")
                    return
                # dotenv uses atomic replacement; no credential is sent to stdout.
                set_key(str(path), "DATABASE_URL", value, quote_mode="always")
                updated = load_settings(path, require_file=True, settings_type=DatabaseProbeSettings)
                if updated.database_url.get_secret_value() != value:
                    raise ConfigurationError("Local runtime configuration verification failed.")
                print("Restricted runtime login verified and saved to local configuration. Direct credential unchanged.")
    except SecurityVerificationFailure as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("Runtime security operation failed. Check configuration and permissions locally; "
              "use provision --rotate for explicit managed-login recovery after a failed local save.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
