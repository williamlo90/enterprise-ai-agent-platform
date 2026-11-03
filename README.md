# Enterprise AI Agent Platform

A reusable, multi-tenant AI platform for company knowledge retrieval and business workflows.

The initial product combines a bilingual travel-policy assistant with reimbursement submission, human approvals, and status tracking for two company tenants. Tenant-specific knowledge, access controls, API connections, and business rules share one application foundation.

## Project status

Product and technical planning are documented and reviewed. Application implementation has not started.

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

## Initial technical direction

React/TypeScript, Python/FastAPI, LangGraph, PostgreSQL/pgvector, private object storage, and durable background workers. Provider selection, dependency versions, deployment sizing, and final acceptance thresholds follow feasibility checks and calibration.

## Local files

Keep credentials, private datasets, raw traces, backups, and local experiments out of Git. The repository contains no application credentials or production data.
