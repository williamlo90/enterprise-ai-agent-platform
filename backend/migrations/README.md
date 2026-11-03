# Migration sources

Executable revisions live in the installed Python package at
`backend/src/enterprise_ai_platform/infrastructure/database/migrations/`.
They are included in the wheel; this directory does not duplicate SQL sources.

Use `enterprise-ai-migrate status`, `verify`, and `upgrade` from the repository
root with `--env-file .env`. See [backend instructions](../README.md#migration-dan-foundation-schema-db-02).
`verify` creates and cleans disposable test schemas; `upgrade` applies the
packaged revision chain to the application schema `platform`.

Append a contiguous `NNNN_name.sql` revision; never edit an applied revision.
These are psycopg SQL templates: `{schema}` and the runner's deterministic
`{access_role}` are identifier placeholders; literal braces must be doubled.
Revision 0003 requires PostgreSQL 16+ and CREATEROLE for its restricted NOLOGIN
access role. LOGIN credentials are provisioned separately, not stored in SQL.
Use qualified objects and transactional SQL. Do not
include BEGIN/COMMIT, ROLLBACK, connection changes, nontransactional operations
or external I/O. Review new revisions and exercise failure rollback before
deployment. Checksums detect changed history; there is no stamp, repair-history
or downgrade command. Expand/contract and production rollout follow in DB-04.
