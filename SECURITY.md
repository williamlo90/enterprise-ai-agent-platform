# Security policy

## Reporting

Report suspected vulnerabilities privately through this repository's GitHub **Security → Report
a vulnerability** feature when available. Otherwise use a private contact channel listed on the
owner's [GitHub profile](https://github.com/williamlo90). Public issues should contain only
sanitized, non-sensitive descriptions. Never post credentials, session material, private company
documents, or a working exploit against an external system.

## Security model

- Cognito establishes identity; the platform owns opaque sessions and current membership authority.
- Browser mutations use CSRF and session-bound context. Company changes and logout clear private state.
- Tenant scope is resolved server-side and reinforced by PostgreSQL RLS and restricted runtime roles.
- Source and conversation reads check current content access, including changed audience or publication.
- Human consent binds the reviewed version; AI output cannot grant approval or service authority.
- The independent service verifies internal context and enforces its own business transitions.
- Stable operation identity, atomic receipts, and reconciliation protect recovery after uncertain outcomes.
- Private export and erasure use approved policy and owning-store authority.

## Deployment responsibilities

Keep public browser routes separate from internal service routes. Configure same-origin HTTPS,
exact identity callbacks, private storage, restricted database roles, and controlled logging.
Secrets belong in private local files or a secret manager, never frontend values or images.
Runtime and migration credentials must remain separate.

The [deployment runbook](deployment/README.md) covers activation, trust rotation, held recovery,
and operator decisions. Prepared infrastructure is distinct from accepted production operation.

## Repository hygiene

Environment files, private keys, generated manifests, raw traces, local engineering records,
and runtime caches are ignored. Example files contain configuration shape rather than credentials.
Public screenshots use fictional data. Dependency and adapted-component notices remain intact.
