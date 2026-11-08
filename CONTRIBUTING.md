# Contribution and review guide

This repository is published for portfolio review. Original source use is governed by the
[source-use notice](LICENSE). For questions or bug reports, describe the expected behavior,
the relevant component, and a sanitized reproduction. Use [private reporting](SECURITY.md)
for security-sensitive findings.

## Working on a change

Read the [local setup guide](docs/GETTING_STARTED.md) and relevant component README. Keep domain
logic independent of transport and provider SDKs. Preserve tenant/current-authority checks,
version-bound consent, stable operation identity, and owning-service transaction boundaries.
Never edit an applied migration; add a forward change to the owning package instead.

Keep private credentials and raw artifacts local. Update the relevant public documentation when
behavior changes. Use descriptive branch names and commit messages.

## Verification

Start with targeted checks and follow the [resource policy](RESOURCE_SAFETY_POLICY.md).
Documentation-only changes use static link/read-back checks. Database probes, provider requests,
cloud changes, and heavy builds require deliberate scope; they are not routine documentation checks.
The [verification guide](TEST_PLAN.md) lists available paths.
