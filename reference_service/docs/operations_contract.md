# REF-03 operation identity, recovery and terminal proof — draft

Scope: reusable service ledger and atomic commit/seal boundary. No claim endpoint,
human consent producer, policy authority or payment effect is supplied here.
CLAIM-01 later adds the claim relation and business state in the same UoW.

## Identity and concurrency

An immutable operation identity contains `tenant_id`, `operation_id`, `actor_id`,
`caller` (`platform-dispatcher`), `kind` (`claim.submit`, `claim.information`, or
`claim.decide`), canonical `request_hash`, UUID `key`, original object
`{object_kind,object_id,object_version}` as flat identity fields. Both `(tenant_id,operation_id)` and
`(tenant_id,kind,actor_id,key)` uniquely identify one operation. The complete
identity must match on replay. A different hash, key, actor, original object or
operation mapping never overwrites an existing result.

Commit and seal acquire the same two transaction-scoped advisory locks: one for
the tenant/operation ID, one for the tenant/kind/actor/key tuple. Hash-derived
signed 64-bit lock IDs are ordered numerically; collisions only serialize extra
work. The locks exist even when the row is absent. After both locks are acquired,
the repository checks both identities, validates any existing binding, and either
returns the terminal result or creates one terminal result in a short transaction.
Unique DB constraints remain the final protection, not an absent-row SELECT lock.

The terminal states are `committed`, `rejected`, `sealed_no_commit`; no durable
half-finished processing state is introduced. A trusted internal feature callback
and result/audit record share the same service transaction. Callback failure,
authority expiry, cancellation or DB failure rolls everything back. An identical
replay returns the old result without invoking the callback or applying current
submission deadlines anew, after current caller/actor authorization. A sealed
identity cannot execute a late callback. Lookup absence returns unknown (404),
never a no-commit assertion. No network exactly-once guarantee is claimed.

## Read and recovery wire proposal

`GET /service/v1/operations/{operation_id}` uses the existing AUTH-04
`operation.read` delegation, with its canonical object ID matching the URL and
object version 1. Its canonical payload is carried in an explicit
`X-EAI-Operation-Payload` base64url header to avoid a GET body; no query arguments,
cookies or duplicate security headers are accepted. This header is private and
must never be logged. The payload's `operation_id` is the verification request ID;
the object ID identifies the existing ledger operation. Caller and current actor
are reverified, then ledger ownership and current tenant scope are checked.

`POST /service/v1/operations/{operation_id}/seal-if-absent` requires a separately
provisioned recovery ingress credential scoped to seal, plus current actor
`operation.read` delegation. Exact JSON body is `{payload,identity}`. The read
payload object must match the URL/version 1; identity must match its actor/tenant
and the target operation. No new AUTH-04 action is added. A recovery credential
alone cannot seal another actor's identity, and ordinary dispatcher credentials
cannot enter the seal path. Existing committed/rejected/sealed results are
returned unchanged after reauthorization. With no result, the exact identity is
sealed transactionally. A denied or unavailable verification performs no write.

Response is `{operation,proof}`. `operation` contains the complete immutable
identity, terminal state, `result` (bounded structured refs, not raw claim/receipt
content), `result_digest`, original `completed_at` epoch and `ledger_revision:1`.
Foreign/unauthorized operation is 404; identity collision is 409; missing/current
auth denied is 401/403; dependency/ambiguous transaction outcome is 503. A 503
requires lookup with the same identity; no client key regeneration or auto replay.

## Terminal proof proposal — requires Lead/QA review

Use a separate service Ed25519 private key and exact operator-distributed
public-key/kid registry at the consumer. No platform delegation signing key reuse.
The existing project `cryptography` pin is used if this proposal is accepted.
Proof envelope is `{payload,signature}` where signature is canonical unpadded
base64url Ed25519 over `reimbursement:terminal-proof:v1:` followed by sorted-key,
compact UTF-8 JSON payload. Payload fields are exact: `schema_version:1`,
`issuer:reference-reimbursement`, `audience:enterprise-ai-platform`, `kid`,
`operation` (full terminal record above), `issued_at`, `expires_at` (the lesser of
60 seconds from issuance and the current actor authorization deadline). Signing
checks the clock again after signature creation. Optional operations API responses
also check this deadline immediately before sending successful response headers,
suppressing the original response buffer on expiry and returning 503 for
same-identity lookup.
DB completion and local issuance clocks may differ by at most five seconds,
matching the verifier's existing clock tolerance. Issuance is the later of the
local epoch second and immutable DB completion second, only within that bound.
Expiry remains capped by **local** issuance-read time plus 60 seconds and by the
actor deadline; clock skew never extends authority. Larger future skew, or no
positive issuance-to-expiry window, returns unavailable and retains the ledger.
Protected operation success JSON is buffered to completion, capped at 64 KiB,
before headers are released. The boundary checks again after awaiting header
send and before any private proof body. If authority expires during that await,
it withholds the body and aborts the response; the caller must treat a truncated
success as unknown and use the same-identity lookup. No expired proof body is
released and no ledger mutation is replayed by this transport recovery.

The signature attests the immutable terminal ledger record; it does not attest
that REF-03 executed a real claim or issued human consent. Minting occurs only
after the UoW commits and after final authorization/deadline checks. Current
authorized lookup/replay can mint a fresh proof for an old immutable result
without rerunning effects. Consumer checks trusted kid/key, signature, exact
issuer/audience/version/shape, expiry, complete expected operation identity and
result digest. It must independently match the claim/file/pin relation once that
relation exists. A valid proof for another operation never authorizes release.
Bare result/proof digest, untrusted embedded key, unknown kid, expired proof or
unknown lookup cannot release pins. Production consumers remain gated until
FILE-02/FLOW-01 connect this protocol and verify current trust/relations. No
offline proof is substituted for current access when reading a result.

Key provisioning/rotation/revocation and service cloud hosting remain operator
integration gates. No private key is generated or persisted by API startup;
tests create temporary in-memory keys. No real proof endpoint is enabled without
explicit service integration configuration and verified actor/recovery wiring.

Lead/QA approved this bounded proof proposal. The default signer/trust registry
is empty and fails closed. Every signed field, including kid and expiry, is
covered by the signature; keys are local trusted configuration with no caller
key/JWK URL. Signing failure after commit does not roll back the ledger or replay
the effect. The client receives unavailable/unknown, then reauthorizes lookup of
the same operation and re-signs the same terminal record. `completed_at` stays
unchanged while proof `issued_at` may advance. A rejected terminal proof alone
does not authorize receipt release. Recovery tenant/actor/caller are derived
from verification/config/ledger and checked against every supplied identity
field. Default object access here is exact operation owner; active-stage/claim
relations remain the consumer's later integration requirement.
