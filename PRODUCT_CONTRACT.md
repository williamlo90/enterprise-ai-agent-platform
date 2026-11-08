# Product contract

## Purpose

Help employees use company knowledge and complete policy-governed travel reimbursement work
inside an authorized company workspace. Support Indonesian and English interactions, inspectable
sources, editable drafts, explicit human decisions, and recoverable processing.

## Users

| User | Responsibility |
| --- | --- |
| Employee | Ask questions, inspect permitted sources, prepare claims, and supply requested information. |
| Knowledge manager | Govern document versions, publication, effective periods, and audiences. |
| Reviewer | Inspect current assigned claims and approve, reject, or request information. |
| Scoped administrator | Manage granted membership, configuration, and business-policy operations. |
| Operator | Configure environments, secrets, deployment, recovery, and model activation. |

Administrative access does not implicitly grant private-content access. Every workflow uses current
membership and object authority, including after a session, role, policy, or source changes.

## Journeys

- Ask a company-policy question, read the cited passage, and return to the conversation.
- Upload and inspect a document, configure its audience, publish a version, and withdraw it.
- Prepare a reimbursement manually or through assistance using the same editable draft.
- Review expenses, receipt versions, business rules, and approval route before confirmation.
- Follow processing status, supply requested information, and inspect the service result.
- Review assigned work and bind a reasoned decision to the exact claim revision.

## Product rules

The model may assist with retrieval and preparation. It cannot consent, approve, change authority,
or bypass service-side rules. An editable draft is distinct from a confirmed action. Changed
details require a fresh preview and consent. An acknowledged job is distinct from a committed
business result; uncertain outcomes remain visible until reconciled.

Knowledge answers must respect tenant, audience, publication, and current-access rules. Receipt
uploads preserve evidence; the product does not claim OCR verification of receipt amounts.

## Delivery scope

The repository includes knowledge, conversation, reimbursement, approval, administration, and
conversation-privacy flows. The AWS deployment package provides infrastructure templates,
container release tooling, and recovery procedures. The business integration uses the project's
independent reimbursement service. Payments and commercial ERP integration are outside scope.

See [architecture](ARCHITECTURE.md), [contracts](DATA_AND_API_CONTRACTS.md), and the
[product tour](docs/product-tour/README.md).
