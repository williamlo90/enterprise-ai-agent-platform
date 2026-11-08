"""Explicit migration command; migration credentials never enter API resources."""
import argparse
import asyncio
import os
from reimbursement_service.composition import database_loop_factory
from reimbursement_service.infrastructure.database.migration import migrate, MigrationError
from reimbursement_service.infrastructure.database.target import parse_target, TargetError


def main():
    parser = argparse.ArgumentParser(description="Service-local migration status or explicit upgrade")
    parser.add_argument("--upgrade", action="store_true")
    args = parser.parse_args()
    try:
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            result = runner.run(migrate(
                parse_target(os.environ.get("REIMBURSEMENT_MIGRATION_DATABASE_URL", ""), direct=True),
                parse_target(os.environ.get("REIMBURSEMENT_DATABASE_URL", "")),
                schema=os.environ.get("REIMBURSEMENT_SCHEMA", "reimbursement"), upgrade=args.upgrade))
    except (MigrationError, TargetError):
        raise SystemExit("Service migration unavailable; no credentials are logged.") from None
    print(f"Service migration current={result[0]} head={result[1]}")


if __name__ == "__main__":
    main()
