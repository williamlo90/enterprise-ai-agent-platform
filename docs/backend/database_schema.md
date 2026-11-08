# Database ownership and isolation

## Platform database

The platform stores identity and membership, scoped tenant configuration, document metadata and
versions, source indexes, conversations and provenance, policies, drafts, human action receipts,
jobs, checkpoints, and privacy lifecycle state. PostgreSQL/pgvector supports persisted knowledge
retrieval alongside transactional application records.

## Reference-service database

The reimbursement service owns its tenant registry, trips, immutable rule packages, claim
revisions, approval stages, operation ledger, and terminal proofs. Its tenant registry establishes
scope and does not independently grant membership. Platform and service credentials are distinct.

## Isolation

Runtime connections use restricted roles and transaction-local actor/tenant context. Row-level
security reinforces tenant boundaries. Runtime roles do not own migration objects or receive
operator privileges. Connection cleanup, transaction rollback, and current-authority checks are
part of the application boundary.

## Migrations

SQL is packaged with the owning application and bound to migration history by checksum. Changes
are forward-only; applied migration source is immutable. API startup does not apply migrations.
The packaged migration history describes available source, not the schema installed on a target.

- [Platform migration source](../../backend/src/enterprise_ai_platform/infrastructure/database/migrations/)
- [Reference-service source and migration package](../../reference_service/src/reimbursement_service/)
- [Operator migration and release procedure](../../deployment/README.md)

## Integrity

Draft versions, policy packages, and action identities preserve reviewable inputs. Owning-store
transactions commit business effects together with their operation receipts. Privacy fences
coordinate inventory and conversation disposition. Operational recovery additionally uses the
independent journal and held-restore procedure supplied in the deployment package.
