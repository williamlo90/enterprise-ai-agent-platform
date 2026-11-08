# REF-04 trips and immutable package receiver

This task stores reference data in the service's own database. Trips/items are
fictional operator-provisioned data, not evaluation seeds or model-writable data.
Packages are immutable copies. Receipt acknowledgement is not policy activation,
eligibility, verified document publication, claim submission or approval.

## Authorization split

Single-trip GET uses existing AUTH-04 `trip.read` with the exact trip object ID,
version 1 and empty data. It checks both current actor and stored tenant/owner.
The private canonical payload is transported in the same bounded base64url
header pattern as operation lookup, named `X-EAI-Trip-Payload`.

AUTH-04 has no trip-list or publisher action. These paths therefore require a
separate explicit trusted `ScopedAuthorityPort` action `trips.list` or
`policy.publish`. Its default adapter denies with 503 and performs no reads or
writes. It is never implemented by renaming `trip.read` or minting a service
grant from user JSON. Actual producer authorization and signed action/contract
integration are a POL-01/AUTH-04 integration gate.

The port receives raw security headers and an immutable service-computed request
binding `{action,object_id,object_version,request_digest,operation_id,key}`. It
must authenticate the configured caller, verify current tenant/actor authority
and return a bound `ScopedAuthority` with exact binding, tenant/actor/caller and
an expiry no longer than 60 seconds. User-supplied role/tenant/owner claims never
substitute for it. Fixture ports are explicit, controlled and test-only. Current
scope, all bindings and expiry are checked before repository work and egress.
The future publisher verifier must establish current process-owner/publisher
authority on the exact reimbursement scope; tenant-admin alone is insufficient.

Local acceptance proves the actual receiver/DB/API with controlled scoped ports,
and default/unbound/deny/outage/expired/misbound ports fail closed. It does not
claim a production publisher or trip-list delegation exists. Accepted REF-02
evidence is reused for the unchanged single-object actor verification component.

## Trips and items

`GET /service/v1/trips/{id}` returns `{id,start_date,end_date,state,employee_ref,
items:[{id,expense_date,category,declared_amount_minor,currency}]}` for the current
owner in the current active tenant. Missing, foreign or colleague trips return
404 without content. There is no public trip/item mutation endpoint.

`GET /service/v1/trips?after=<owned trip UUID>&limit=<1..50>` uses `trips.list`,
returns `{items:[trip summaries without expense items],next_cursor:UUID|null}`.
Omitted limit is 20; no total count. UUID ordering is stable. A cursor must exist
inside the current tenant/owner result scope; unknown/foreign cursors return 404.
Unknown/duplicate query keys and malformed IDs/limits are invalid. Filtering and
pagination occur after ownership, never by fetching a global page then filtering.
List binding uses `object_id:null,object_version:1`, a fresh UUID
`X-EAI-Operation-ID`, and SHA256 domain `reimbursement:trip-list:v1:` over compact,
sorted UTF-8 `{after:<UUID|null>,limit:<int>}`. It has no mutation key.

Operator seed validates exact fields and same-tenant item/trip FK/owner, canonical
dates with end >= start, canonical item dates, state `completed`, bounded
employee reference/category codes, positive integer minor amounts <= signed
64-bit maximum, and currencies IDR/SGD/USD. A trip has 1..100 fixture items. These
are initial transport/resource bounds; claims impose their own business rules.
No OCR or verification of employee-declared values is inferred.
Date/category eligibility is a later policy decision; a reference expense date
is not silently constrained to the travel interval (for example advance booking).

## Rule package receiving contract

`PUT /service/v1/policy-packages/{id}/versions/{positive integer}` requires
`policy.publish`, JSON body below, `Idempotency-Key:<UUID>`, and
`X-EAI-Operation-ID:<UUID>`. Unknown fields are rejected. No tenant/actor/publisher
role in body. Tenant derives from the trusted authorization port. Request binding
digest is the exact canonical body digest plus URL package ID/version; publisher
authorization must bind those bytes before a write is attempted.

Body fields: `schema_version:1`, `digest:sha256`, `valid_trip_end_from:YYYY-MM-DD`,
`valid_trip_end_to:YYYY-MM-DD|null` (exclusive), `timezone:IANA`,
`document_versions:[{document_id:UUID,version_id:UUID,content_digest:sha256}]`,
`rules:{currency:IDR|SGD|USD,submission_deadline_days:0..3650,
max_total_minor:positive integer|null,
categories:[{code:lowercase code,receipt_required:boolean,max_amount_minor:positive integer|null}],
approval_route:[{stage:1..10,approver_id:UUID,scope_ref:reimbursement}]}`.

The range is nonempty; timezone must be available in the pinned tzdata/IANA
catalog. Document refs: 1..10 unique versions; categories: 1..100 unique codes;
route: 1..10 contiguous ordered stages. JSON is <=64 KiB. The 3650-day/resource
ceilings are initial technical validation envelopes, not chosen tenant business
values. Root 30/14-day examples are not hardcoded. No floats/bool-as-integer,
duplicate keys, silent truncation, arbitrary rules expressions or unknown fields.

Package digest is SHA256 over `reimbursement:rule-package:v1:` followed by
sorted-key compact UTF-8 canonical JSON of `{schema_version,tenant_id,package_id,
package_version,valid_trip_end_from,valid_trip_end_to,timezone,document_versions,
rules}`. Category and document sets are sorted by code/version ID; approval route
order is preserved. This receiver format is frozen locally; POL-01 must implement
it or introduce an explicitly reviewed version. No existing producer was changed. Receiver validates
the provided digest after tenant/actor authorization, stores the exact immutable
canonical content and a stable acknowledgement `{package_id,package_version,
digest,received_at,state:received}`. It never returns `active` or `eligible`.

Tenant+package+version is unique. Same version and digest/content is acknowledged
unchanged; conflicting content/digest returns 409. Idempotency receipts are keyed
by tenant+actor+UUID key and bind the complete request material. Same key/material
returns the original receipt after current reauthorization; changed material or
URL with the same key returns 409. Key receipts and immutable copy commit in the
same transaction with expiry rechecked before commit/egress. Different keys do
not permit overwriting a copied version. Unknown outcomes preserve key/material.
Valid package ranges may overlap among **received copies**: publication/active
selection and known-conflict/withdraw eligibility remain live POL-01 behavior,
not a cached boolean inferred from a copy or its dates.
Logical operation UUID is also bound in the receipt: same key with a different
operation, or same operation with a different key, conflicts. `X-Request-ID` is
always generated by the service for tracing, independently of this logical
operation header; caller tracing headers confer no authority.

Service migration 0003 is additive; accepted 0001/0002 remain unchanged. All new
tables are private forced-RLS behind guarded scoped capabilities. Seeds use a
separate operator connection, never the runtime API, and preserve accepted data.
