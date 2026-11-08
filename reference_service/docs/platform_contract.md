# REF-02 platform HTTP contracts

Actor verification uses the existing AUTH-04 wire unchanged. The service sends
`POST /internal/v1/actors/verify`, exact JSON `{payload:<canonical v1 object>}`,
its own verifier bearer credential and the ingress `X-EAI-Delegation` JWT.
The separate ingress bearer credential authenticates `platform-dispatcher`.
The operator must bind the verifier credential registry to that dispatcher and
audience `reference-reimbursement`; these are different credentials.

The actor response has exactly `allow`, `scope`, `actor_id`, `tenant_id`,
`membership_id`, `access_revision`, `action`, `operation_id`, `request_hash`,
`verified_at`, `expires_at`, `replayed`. Scope must be `actor_only`; allow must
be true. IDs, action, operation and canonical request digest must match the
request. Integers are strict integers, not booleans. Expiry must be future and
within 60 seconds of verification. Every invocation contacts the platform;
there is no actor cache. Business modules must reverify before effects and
enforce current owner/stage/state/receipt relations themselves.
Ingress authorization additionally checks the injected clock after all final
binding/hash checks immediately before returning. Exact expiry denies access,
including when time advances during the final canonical hash computation.

No runtime service module imports platform source. The service independently
implements canonical v1 validation and tests its bytes against platform golden
vectors in the external integration helper. Canonical changes require explicit
version review. Human receipt issuance remains ACT-01. The actor integration
uses read actions and tests the unbound receipt-reader hold for human mutations.

## Proposed policy/file producer wire — pending POL-01 / FILE-02 / FLOW-01

The root contract fixes routes and behavior, but not exact DTOs for these
producers. The following is a proposed strict fixture contract, not a claim that
platform endpoints exist or that new AUTH-04 actions are accepted. Clients for
this proposal are disabled by default and require an explicit fixture opt-in.
They are not attached to production composition or exposed through service API.

Proposed request envelope (all fields required, no extra keys):

`{contract_version:1, operation_id:UUID, tenant_id:UUID, actor_id:UUID,
action:string, object:{kind:string,id:UUID,version:positive integer},
request_digest:sha256, deadline:epoch integer, data:object}`.

`request_digest` is SHA256 of `reimbursement:platform-fixture:v1:` plus canonical
sorted-key UTF-8 JSON of the envelope **without** `request_digest`. Deadline is
future and at most 60 seconds from the caller's clock. The immutable envelope is
copied before sending. Responses echo all fields except `data` and `deadline`,
then add `allow:boolean, checked_at:epoch, expires_at:epoch, result:object`.
Every echoed binding must match; `checked_at` is not in the future beyond five
seconds; response expiry is future, at most the request deadline and at most
60 seconds after `checked_at`. An allow=false response denies/holds the action.
No successful fixture response is accepted as a business commit or human grant.

| Route | Proposed action/object/data | Successful result |
| --- | --- | --- |
| `/internal/v1/policy-packages/{id}/eligibility` | `policy.eligibility` / `policy_package` / `{digest:sha256, operation_kind:submission\|information\|decision, trip_date:YYYY-MM-DD, claim_ref:UUID\|null}` | `{eligibility_revision:positive integer}` |
| `/internal/v1/receipt-pins` | `receipt.pin` / `claim` / `{receipts:[{id:UUID,version:int,digest:sha256}]}`; object is the candidate claim | `{attestations:[{id,version,digest,attestation_digest:sha256}]}`; exact receipt set, max 10, no duplicate IDs |
| `/internal/v1/receipt-pins/{operation_id}/finalise` | `receipt.finalise` / `claim` / `{outcome:committed\|sealed_no_commit, proof_digest:sha256}` | `{state:finalised\|released}` matching outcome |

Pin identity is preserved on unknown outcomes; the client never automatically
retries any POST or substitutes a new operation/key. Finalisation requires
durable service commit/seal proof from later tasks. The fixture proposal does
not create that proof, resolve unknown results or release pins on timeout.
For eligibility, `submission` requires `claim_ref:null`; `information` and
`decision` require a nonnull canonical claim UUID. The `proof_digest` in finalise
is solely a transport placeholder. It cannot authorize release and is not a
verifiable commit/seal proof. REF-03/FLOW-01 must define producer-verifiable proof
without callback recursion before any production finalisation is enabled. REF-02
does not introduce new proof cryptography or infer authority from a digest.

Transport is HTTPS to one operator-configured origin; proxy environment and
redirects are disabled. Total request budget is six seconds, network timeout
five seconds, response <=64 KiB, identity encoding only, JSON duplicate keys,
NaN and extra fields rejected. No request body, bearer, JWT or dependency error
body is logged. Typed failures map 400/401/403/409, temporary network/429/502/503/
504 to unavailable, other HTTP or malformed success to protocol failure. All
failures hold access/effects. No automatic request replay occurs.
