"""Explicit migration command. API startup never invokes migrations."""

import argparse
import asyncio
from pathlib import Path
import sys

from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import ConfigurationError, MigrationSettings, load_settings
from enterprise_ai_platform.infrastructure.database.connection import DatabaseConfigurationError, parse_target
from enterprise_ai_platform.infrastructure.database.migration import migrate
from enterprise_ai_platform.infrastructure.database.migration_probe import VerificationFailure, verify_migrations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "upgrade", "verify"))
    parser.add_argument("--target-version",type=int,help="Stop at a reviewed release boundary; never performs a downgrade.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--env-file", type=Path)
    source.add_argument("--no-env-file", action="store_true")
    args = parser.parse_args()
    if args.command=="verify" and args.target_version is not None:
        parser.error("Verification always checks the complete packaged history.")
    try:
        settings = load_settings(None if args.no_env_file else args.env_file or Path(".env"),
                                 require_file=args.env_file is not None, settings_type=MigrationSettings)
        target = parse_target(settings.database_direct_url.get_secret_value(), direct=True)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            if args.command == "verify":
                count = runner.run(verify_migrations(target))
                print(f"Migration verification passed: {count} checks; disposable schema removed.")
            else:
                current, head = runner.run(migrate(target, upgrade=args.command == "upgrade",target_version=args.target_version))
                boundary="target" if args.target_version is not None else "head"
                print(f"Migration {args.command}: revision {current:04d}, {boundary} {head:04d}.")
    except (ConfigurationError, DatabaseConfigurationError):
        print("Migration configuration missing or invalid.", file=sys.stderr)
        raise SystemExit(2) from None
    except VerificationFailure as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("Migration operation failed. Check database state and permissions locally.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
