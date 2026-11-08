# Reimbursement reference service

An independently packaged FastAPI service owns trips, immutable business-rule packages, claims,
approval stages, revisions, and durable operation receipts. It enforces business authority even
when the caller is the platform worker. It has its own database credentials and migrations and
does not import platform source or share ORM objects.

## Setup

Use Python 3.13 and the service lockfile. From the repository root:

```powershell
python -m uv sync --locked --project reference_service
```

Configuration comes from the process environment; the service does not automatically load the
platform `.env`. Use dedicated runtime and operator migration credentials. Set
`REIMBURSEMENT_DATABASE_URL` and the private caller/actor verification configuration required by
the connected deployment. Operator provisioning registers tenant scope, immutable policy
packages, and authorized callers.

```powershell
python -m uv run --locked --project reference_service reimbursement-api
```

The API requires a configured database with matching schema and restricted runtime role.
Startup checks the configured database and fails closed on invalid authority or schema state.
Migration is explicit operator work, never an API-startup side effect. Follow the
[deployment runbook](../deployment/README.md) for the complete connected configuration.

## Transaction model

- Signed internal context binds caller, tenant, actor, purpose, and operation content.
- Current owner/reviewer authority and current claim revision govern each mutation.
- Stable operation identity supports exact replay and rejects conflicting content.
- Claim effects and operation receipts commit atomically in the service-owned transaction.
- Unknown outcomes require lookup/reconciliation rather than blind mutation retries.
- Immutable rule packages preserve the policy governing a submitted claim.

Health endpoints are `/health/live` and `/health/ready`. Private business routes remain internal.
Errors are sanitized, responses use no-store semantics, and default access logging is disabled.

See [data and API contracts](../DATA_AND_API_CONTRACTS.md), [system architecture](../ARCHITECTURE.md),
and the [verification guide](../TEST_PLAN.md).
