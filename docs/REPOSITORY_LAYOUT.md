# Repository layout

```text
frontend/           React/TypeScript workspace, runtime tooling, and browser checks
backend/            Platform API, workers, modules, adapters, migrations, and tests
reference_service/  Independent reimbursement business service and its own lockfile
scripts/            Frontend tooling, explicit checks, evaluation runners, and operator utilities
evaluation/         Evaluation configuration and fixture inputs
evaluations/        Evaluation workspace conventions
infra/              AWS definitions, IAM examples, and independent journal handler
deployment/         Container definitions, routing, release tooling, and recovery runbook
docs/               Curated product, architecture, setup, and portfolio documentation
.github/workflows/  Manually invoked repository and deployment workflows
```

The root [product contract](../PRODUCT_CONTRACT.md), [architecture](../ARCHITECTURE.md),
[data/API contracts](../DATA_AND_API_CONTRACTS.md), and [verification guide](../TEST_PLAN.md)
describe the current system. Each application owns its dependency manifest and lockfile.

API routes consume application services. Domain/port modules are independent of transport and
provider SDKs. Infrastructure implements those ports. The independent reimbursement service
does not import platform source. Private credentials, local runtime files, experiments, and
raw engineering records stay outside the published repository.
