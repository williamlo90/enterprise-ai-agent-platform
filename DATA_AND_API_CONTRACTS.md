# Data and API contracts

## Ownership

| Owner | Data and authority |
| --- | --- |
| Identity and tenancy | Principals, sessions, memberships, tenant scope, and scoped configuration. |
| Knowledge and files | Document versions, audience rules, extracted text, indexes, private objects, and receipt pins. |
| Conversations | Owned messages, feedback, provenance bindings, and assisted-run references. |
| Policies and drafts | Immutable rule packages, eligibility, editable drafts, and reviewed draft versions. |
| Actions and jobs | Human consent, stable operation identity, durable dispatch, leases, and reconciliation. |
| Reference service | Trips, claim state, revisions, approval stages, business decisions, and operation receipts. |
| Privacy | Approved lifecycle policy, subject inventory, fences, erasure/export intent, and disposition records. |

## Browser contract

Protected routes resolve identity from an opaque session and current server-owned membership.
Tenant or actor identifiers in a browser payload are not authority. Browser context and CSRF
protections bind mutations to the active session. A change of company or logout clears private
workspace state. The server checks object access before returning private content.

HTTP errors use a sanitized envelope with `code`, `message`, `request_id`, and `retryable`.
The server generates the request identifier. Input schemas reject unsupported fields; transport
boundaries constrain body handling. Private responses use no-store semantics.

## Knowledge and provenance

Retrieval uses current tenant, audience, publication, effective-period, and source-version rules.
Citations identify source passages. Opening a source and reading conversation history requires
current authorization; cached provenance never becomes a permanent access grant.

## Human actions

Manual and assisted preparation share draft validation. Preview identifies the exact draft,
receipts, applicable rules, and approval route. Confirmation produces a version-bound human
action receipt. A changed draft, stale policy, revoked session, or lost authority cannot reuse
old consent to authorize new work.

## Internal operations

The reference service independently verifies signed caller/delegation context and current
business authority. Operation keys bind an action to its canonical content. Exact replay uses
the existing operation; conflicting content is rejected. Database changes and service receipts
commit atomically within the owning service.

Unknown results stay unknown until authoritative lookup or reconciliation establishes the
outcome. A timeout is not permission to create another business operation.

## Privacy

Lifecycle operations bind policy, subject authority, inventory, and current store state. Holds,
minimum retention, region uncertainty, unresolved effects, and revoked authority deny unsafe
processing. Conversation erasure and private export use owning-store controls; enabling additional
categories requires their own adapters and operational policy.

## Executable references

- [Platform routes](backend/src/enterprise_ai_platform/api/)
- [Domain modules and ports](backend/src/enterprise_ai_platform/modules/)
- [Platform migrations](backend/src/enterprise_ai_platform/infrastructure/database/migrations/)
- [Reference-service source](reference_service/src/reimbursement_service/)
- [Platform contract fixtures](backend/tests/contracts/)
- [Verification strategy](TEST_PLAN.md)
