# Enterprise AI Agent Platform

A reusable, multi-tenant AI platform for company knowledge retrieval and business workflows.

The initial product combines a bilingual travel-policy assistant with reimbursement submission, human approvals, and status tracking for two company tenants. Tenant-specific knowledge, access controls, API connections, and business rules share one application foundation.

## Project status

The API foundation provides health/readiness, consistent errors, request IDs, bounded input handling, and application-local service/port wiring. PostgreSQL migrations are applied through revision **0003**, with a separate restricted runtime login, transaction-local tenant/actor context, and row-level security on identity/config data.

**189 tests pass**, including three live database integrations. Independent QA, clean wheel installation, 40 migration checks and 55 runtime isolation checks passed. Authentication and business workflows remain planned. See [backend setup](backend/README.md) and [delivery evidence](docs/backend/task_plan.md).

## Planned MVP

- Tenant-isolated knowledge retrieval with source citations, document publishing, updates, and withdrawal.
- Indonesian and English interactions with role-based access controls.
- Reimbursement drafts, explicit confirmation, API submission, human approvals, and status tracking.
- Persistent execution state, safe retries, recovery, and independent enforcement by a reference business service.
- Automated evaluation, security checks, tracing, and latency/cost measurement.

Reimbursement payments are outside the MVP. Cloud operations and open-source inference are subsequent milestones. The reference reimbursement service will be built for this project; integration with a commercial ERP is not claimed.

## Documentation

| Document | Purpose |
| --- | --- |
| [Product contract](PRODUCT_CONTRACT.md) | Problems, users, scenarios, MVP boundaries, business rules, and delivery milestones. |
| [Architecture](ARCHITECTURE.md) | Components, responsibilities, trust boundaries, data flows, and deployment. |
| [Data and API contracts](DATA_AND_API_CONTRACTS.md) | Data ownership, API behavior, authorization, and state transitions. |
| [Test plan](TEST_PLAN.md) | Requirement coverage, initial 80-case RAG and 24-scenario workflow packs, critical suites, and manual–assisted pilot. |
| [Repository layout](docs/REPOSITORY_LAYOUT.md) | Folder responsibilities, module boundaries, test placement, and bootstrap conventions. |
| [Backend architecture](docs/backend/architecture.md) | Package boundaries, dependency rules, composition, transactions, and worker execution. |
| [Database schema](docs/backend/database_schema.md) | Tables, types, relationships, constraints, indexes, tenant isolation, and migration phases. |
| [Backend task plan](docs/backend/task_plan.md) | Task IDs, prerequisites, completion evidence, and milestones; implementation starts with BE-01. |

## Repository structure

```text
frontend/           Web application
backend/            Platform API and worker, one Python package
reference_service/  Independent reimbursement service
contracts/          Future executable API and schema contracts
tests/              Cross-system E2E, security, and resilience checks
evaluations/        Datasets, rubrics, runners, and manifests
scripts/            Development, checks, and operations
deploy/             Containers, local compose, Kubernetes, and IaC
docs/               Layout, decisions, and runbooks
.github/workflows/  Future CI/CD workflows
```

Empty directories contain `.gitkeep` markers. The platform API now has a runnable entry point, dependency manifest, and lockfile. Worker, reference service, migrations, and deployment configuration follow their implementation stages.

## Initial technical direction

React/TypeScript, Python/FastAPI, LangGraph, PostgreSQL/pgvector, private object storage, and durable background workers. Provider selection, dependency versions, deployment sizing, and final acceptance thresholds follow feasibility checks and calibration.

## Local files

Keep credentials, private datasets, raw traces, backups, and local experiments out of Git. The repository contains no application credentials or production data.
