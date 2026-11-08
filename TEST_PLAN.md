# Verification guide

## Approach

Verification follows product boundaries: tenant isolation, current identity, source access,
reviewed consent, owning-service transactions, durable recovery, and private-data disposition.
Choose checks for the code being changed, keep files serial, and reuse applicable evidence.
Follow the [resource policy](RESOURCE_SAFETY_POLICY.md).

| Area | Behavior to inspect |
| --- | --- |
| API foundation | Sanitized errors, request identity, bounded inputs, lifecycle, and configured readiness. |
| Identity | Session validity, CSRF, company switching, role changes, and revocation. |
| Knowledge | Versioning, audience rules, publication/withdrawal, citations, and current source access. |
| Drafts and approvals | Shared preparation, stale-review rejection, exact consent, and current reviewer authority. |
| Execution | Lease fencing, exact replay, atomic receipts, and unknown-outcome reconciliation. |
| Privacy | Policy binding, retention/hold denial, inventory fences, erasure, and current-authority export. |

## Developer commands

These commands describe available checks; they are not automatic setup steps or a claim that a
fresh verification was performed for the publication change.

```powershell
python -m uv run --locked --project backend pytest backend/tests/unit/test_architecture.py
python scripts/checks/check_architecture.py
python -m uv run --locked --project reference_service pytest reference_service/tests/unit
& ./scripts/frontend/run-npm.ps1 -NpmArguments @('run', 'test:unit')
```

Select relevant test files before expanding scope. Frontend typecheck/lint/build commands are
documented in the [frontend guide](frontend/README.md).

## Integration opt-ins

Database, provider, storage, browser, and cloud checks require their configured environment and
explicit execution. Never infer provider quality or operational readiness from controlled fixtures.
Do not run database probes, inference, full suites, or deployment as a side effect of documentation
maintenance. Model-quality and deployment acceptance are operator-owned activities.

## Repository presentation checks

`python scripts/checks/check_public_docs.py` checks published Markdown links, image references,
and required repository entry points using only the local Git index. It does not start services
or contact external systems. The corresponding GitHub workflow is manually invoked.
