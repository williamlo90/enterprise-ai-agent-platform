# Portfolio application pack

## Short description

Built a multi-tenant enterprise AI workspace that connects cited company-knowledge retrieval
with human-approved reimbursement workflows, using React, FastAPI, LangGraph, and PostgreSQL.

## CV bullets

- Built a bilingual enterprise workspace for policy search, source inspection, document governance,
  reimbursement preparation, human approvals, and scoped administration.
- Implemented Cognito-backed server sessions, CSRF protection, current-authority checks, and
  tenant isolation with PostgreSQL row-level security.
- Connected manual and AI-assisted preparation to a shared version-bound review and consent flow.
- Designed durable execution with checkpointing, lease fencing, idempotent actions, and
  authoritative reconciliation for uncertain business outcomes.
- Built an independent reimbursement service with separate credentials, migration ownership,
  business-rule enforcement, and atomic operation receipts.
- Prepared container, AWS infrastructure, private routing, independent journaling, and recovery tooling.

## LinkedIn or portfolio summary

Enterprise AI Agent Platform turns company knowledge into a reviewable business workflow.
Employees can ask a question, inspect the cited policy, prepare a reimbursement, and follow
human review in the same workspace. The engineering focus is keeping assistance connected
to current permissions, explicit consent, durable execution, and service-owned transaction truth.

## Interview talking points

| Topic | Decision to explain |
| --- | --- |
| Product design | Why policy retrieval and business execution belong in a connected workflow. |
| AI boundaries | Why model output cannot grant permission, consent, or approval. |
| Security | How session, tenant, current membership, and source access interact. |
| Transactions | Why an independent service owns claim effects and durable receipts. |
| Reliability | How operation identity and lookup recover an unknown outcome safely. |
| Architecture | How ports and adapters keep domain logic independent of infrastructure. |
| Operations | Why deployment preparation and operator acceptance are distinct. |

See the [case study](CASE_STUDY.md) and [product tour](../product-tour/README.md).
