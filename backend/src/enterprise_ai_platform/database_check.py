"""Database probe entry point: validates by default; --live explicitly writes."""

import argparse
import asyncio
from pathlib import Path
import sys

from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import ConfigurationError, DatabaseProbeSettings, load_settings
from enterprise_ai_platform.infrastructure.database.connection import (
    DatabaseConfigurationError, DatabaseUnavailable, parse_target, verify_same_database,
)
from enterprise_ai_platform.infrastructure.database_probe import run_probe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--env-file", type=Path)
    source.add_argument("--no-env-file", action="store_true")
    parser.add_argument("--live", action="store_true", help="Use a dedicated test database: creates and drops a probe table.")
    args = parser.parse_args()
    try:
        settings = load_settings(None if args.no_env_file else args.env_file or Path(".env"),
                                 require_file=args.env_file is not None, settings_type=DatabaseProbeSettings)
        runtime = parse_target(settings.database_url.get_secret_value())
        direct = parse_target(settings.database_direct_url.get_secret_value(), direct=True)
        verify_same_database(runtime, direct)
        if not args.live:
            print("Database configuration valid. No database connection made.")
            return
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            result = runner.run(run_probe(runtime, direct))
        if not all(result.values()):
            raise DatabaseUnavailable()
        print("Database checks passed: TLS, commit, rollback, error reuse, cancellation, deadline, shutdown and probe cleanup.")
    except (ConfigurationError, DatabaseConfigurationError):
        print("Database configuration missing or invalid.", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        # Driver/DDL/network diagnostics must never reach CLI logs or chat.
        print("Database checks failed. Check connectivity and dedicated test database permissions locally.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
