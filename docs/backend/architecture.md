# Backend architecture

The platform is a modular application shared by API and worker processes. The reimbursement
service is an independent business authority with separate packaging and database ownership.

## Dependency direction

```text
Entry points and composition
    → API transport → application services → domain and ports
    → infrastructure and integrations → implementations of ports
```

Domain and port modules use provider-neutral types. Services coordinate public module contracts;
routes validate transport data and call services. Infrastructure implements storage and network
ports. The composition root supplies resources and lifecycle. Request/job identity and
transactions are scoped rather than shared mutable global state.

## Modules

Identity resolves sessions and current membership. Knowledge coordinates documents, audiences,
retrieval, and source access through file/storage ports. Conversations own message sequencing
and provenance-aware reads. Policies own immutable business packages and current eligibility.
Drafts own editable preparation and reviewed versions. Actions own human consent and stable
operation identity; jobs and checkpoints preserve execution and recovery state. Administration
and privacy use explicit scoped authority rather than implicit tenant-wide content access.

## Transactions and remote work

Platform repositories participate in a shared unit of work where atomic local effects are
required. The reference service owns its business transaction and receipt. Durable leases and
operation identities coordinate across the network; service lookup resolves uncertain outcomes.
Remote calls do not replace current-authority checks before protected reads or writes.

## Source navigation

- [API boundary](../../backend/src/enterprise_ai_platform/api/)
- [Modules](../../backend/src/enterprise_ai_platform/modules/)
- [Database adapters](../../backend/src/enterprise_ai_platform/infrastructure/database/)
- [Composition](../../backend/src/enterprise_ai_platform/composition.py)
- [Architecture guard](../../scripts/checks/check_architecture.py)
- [Independent service](../../reference_service/src/reimbursement_service/)

See [system architecture](../../ARCHITECTURE.md) and [data contracts](../../DATA_AND_API_CONTRACTS.md).
