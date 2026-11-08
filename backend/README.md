# Platform backend

The FastAPI platform owns identity, knowledge, conversations, policies, drafts, human-action
receipts, durable jobs, scoped administration, and conversation privacy. API and worker processes
share application services; transport and infrastructure remain separate from domain modules.

## Setup

Use Python 3.13 and uv. From the repository root:

```powershell
python -m uv sync --locked --project backend
python -m uv run --locked --project backend enterprise-ai-api --env-file .env.example --check-config
python -m uv run --locked --project backend enterprise-ai-api --env-file .env.example
```

The default configuration starts the API foundation without database, identity, or provider
activation. Development OpenAPI is available at `http://127.0.0.1:8000/docs`.
`/health/live` reports process liveness; `/health/ready` includes configured runtime dependencies.
Business routes are composed when their required services are enabled and configured.

Use a private `.env` for connected environments. Process environment values override file values.
Configuration failures and HTTP errors are sanitized. Keep runtime credentials separate from
operator migration credentials; an owner login is not a runtime identity.

## Code map

| Location | Responsibility |
| --- | --- |
| `api/` | Public and internal transports, schemas, and request boundaries. |
| `modules/` | Domain types, services, and public ports. |
| `infrastructure/` | Database repositories, checkpoint storage, and runtime adapters. |
| `integrations/` | Identity, model, storage, journal, and reimbursement adapters. |
| `composition.py` | Service wiring and lifecycle. |
| `deployment_worker.py` | Configured serial deployment-worker entry point. |

The [architecture guide](../docs/backend/architecture.md) explains dependency rules. The
[database guide](../docs/backend/database_schema.md) covers ownership and migrations.

## Connected operation

Identity, browser context, storage, provider use, privacy, and background execution are explicitly
configured. Startup does not apply migrations or make model requests automatically. Same-origin
HTTPS, Cognito callback configuration, restricted database roles, registered tenants/callers, and
the private reference service are established through the [operator runbook](../deployment/README.md).

See [local setup](../docs/GETTING_STARTED.md), [security](../SECURITY.md), and
[verification](../TEST_PLAN.md). Keep model and database checks deliberate and within the
[resource policy](../RESOURCE_SAFETY_POLICY.md).
