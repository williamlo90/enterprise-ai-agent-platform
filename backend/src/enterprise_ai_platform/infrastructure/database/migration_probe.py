"""Live foundation checks in disposable schemas, never in the application schema."""

import asyncio
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from enterprise_ai_platform.infrastructure.database.connection import bounded_transaction, direct_connection
from enterprise_ai_platform.infrastructure.database.migration import MigrationError, Revision, migrate, revisions
from enterprise_ai_platform.infrastructure.database.roles import drop_probe_schema


class VerificationFailure(RuntimeError):
    def __init__(self, case):
        super().__init__("Migration verification failed: " + case + ".")


def require(condition, case):
    if not condition:
        raise VerificationFailure(case)


async def verify_migrations(target) -> int:
    completed = []
    head = len(revisions())
    owned = []
    stage = "initialization"

    def query(text, schema):
        return sql.SQL(text).format(s=sql.Identifier(schema))

    async def new_schema():
        name = "db02_probe_" + uuid4().hex
        async with direct_connection(target) as connection:
            cursor = await connection.execute("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=%s", (name,))
            require(await cursor.fetchone() is None, "probe-name-unused")
        owned.append(name)
        return name

    async def rejected(schema, case, statement, params, state):
        async with direct_connection(target) as connection:
            try:
                async with bounded_transaction(connection):
                    await connection.execute(query(statement, schema), params)
            except psycopg.Error as exc:
                require(exc.sqlstate == state, case + "-sqlstate")
            else:
                raise VerificationFailure(case)
        completed.append(case)

    async def expect_migration_failure(schema, items=None):
        try:
            await migrate(target, schema=schema, items=items, upgrade=True)
        except MigrationError:
            return
        raise VerificationFailure(stage)

    try:
        schema = await new_schema()
        stage = "read-only-status"
        require(await migrate(target, schema=schema) == (0, head), stage)
        async with direct_connection(target) as connection:
            cursor = await connection.execute("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=%s", (schema,))
            require(await cursor.fetchone() is None, stage)
        completed.append(stage)

        stage = "previous-revision"
        require(await migrate(target, schema=schema, upgrade=True, target_version=1) == (1, 1), stage)
        principal, other, tenant, second, membership = [uuid4() for _ in range(5)]
        stage = "initial-tenant-and-snapshot"
        async with direct_connection(target) as connection:
            async with bounded_transaction(connection):
                for person in (principal, other):
                    await connection.execute(query("INSERT INTO {s}.principals(id,issuer,subject) VALUES (%s,%s,%s)", schema),
                                             (person, "https://identity.example.test", str(person)))
                for owner, tenant_id, slug in ((principal, tenant, "fixture-a"), (other, second, "fixture-b")):
                    await connection.execute(query("INSERT INTO {s}.tenants(id,slug,name,timezone) VALUES (%s,%s,%s,%s)", schema),
                                             (tenant_id, slug, "Fictional Company", "Asia/Singapore"))
                    await connection.execute(query("INSERT INTO {s}.tenant_configs(tenant_id,id,config_version,assistant_name,locales,"
                        "help_contact,instruction_text,connector_ref,model_policy_ref,schema_version) "
                        "VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,1)", schema),
                        (tenant_id, uuid4(), "Fixture assistant", Jsonb(["id", "en"]), "Contact administrator",
                         "Use approved sources.", "fixture-connector", "fixture-model"))
                    await connection.execute(query("INSERT INTO {s}.memberships(tenant_id,id,principal_id) VALUES (%s,%s,%s)", schema),
                                             (tenant_id, membership if owner == principal else uuid4(), owner))
                await connection.execute(query("INSERT INTO {s}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) "
                    "VALUES (%s,%s,%s,'employee','tenant','self',%s)", schema), (tenant, uuid4(), membership, other))
                await connection.execute(query("INSERT INTO {s}.sessions(id,principal_id,token_hash,csrf_hash,active_tenant_id,expires_at) "
                    "VALUES (%s,%s,%s,%s,%s,CURRENT_TIMESTAMP+interval '1 hour')", schema),
                    (uuid4(), principal, "fixture-token-hash", "fixture-csrf-hash", tenant))
        completed.append(stage)

        stage = "filled-upgrade-to-head"
        require(await migrate(target, schema=schema, upgrade=True) == (head, head), stage)
        async with direct_connection(target) as connection:
            require(await (await connection.execute(query("SELECT count(*) FROM {s}.tenant_configs", schema))).fetchone() == (2,), stage)
        completed.append(stage)
        stage = "replay-upgrade"
        require(await migrate(target, schema=schema, upgrade=True) == (head, head), stage)
        completed.append(stage)

        # Deferred FK must fail when the transaction commits, not on INSERT.
        stage = "missing-snapshot-at-commit"
        async with direct_connection(target) as connection:
            inserted = False
            try:
                async with bounded_transaction(connection):
                    await connection.execute(query("INSERT INTO {s}.tenants(id,slug,name,timezone) VALUES (%s,'missing','Missing','UTC')", schema), (uuid4(),))
                    inserted = True
            except psycopg.errors.ForeignKeyViolation:
                require(inserted, stage)
            else:
                raise VerificationFailure(stage)
        completed.append(stage)

        stage = "wrong-tenant-snapshot-at-commit"
        async with direct_connection(target) as connection:
            # Only tenant B has config version 2. Tenant A cannot reference it.
            await connection.execute(query("INSERT INTO {s}.tenant_configs SELECT tenant_id,%s,2,assistant_name,locales,help_contact,"
                "instruction_text,connector_ref,model_policy_ref,schema_version,created_at FROM {s}.tenant_configs WHERE tenant_id=%s", schema), (uuid4(), second))
        await rejected(schema, stage, "UPDATE {s}.tenants SET config_version=2 WHERE id=%s", (tenant,), "23503")

        cases = [
            ("cross-tenant-role", "INSERT INTO {s}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) VALUES (%s,%s,%s,'employee','tenant','self',%s)", (second, uuid4(), membership, other), "23503"),
            ("unknown-role", "INSERT INTO {s}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) VALUES (%s,%s,%s,'superuser','tenant','self',%s)", (tenant, uuid4(), membership, other), "23514"),
            ("duplicate-role", "INSERT INTO {s}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) VALUES (%s,%s,%s,'employee','tenant','self',%s)", (tenant, uuid4(), membership, other), "23505"),
            ("duplicate-membership", "INSERT INTO {s}.memberships(tenant_id,id,principal_id) VALUES (%s,%s,%s)", (tenant, uuid4(), principal), "23505"),
            ("missing-principal", "INSERT INTO {s}.memberships(tenant_id,id,principal_id) VALUES (%s,%s,%s)", (tenant, uuid4(), uuid4()), "23503"),
            ("missing-grantor", "INSERT INTO {s}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) VALUES (%s,%s,%s,'approver','tenant','self',%s)", (tenant, uuid4(), membership, uuid4()), "23503"),
            ("missing-tenant", "INSERT INTO {s}.memberships(tenant_id,id,principal_id) VALUES (%s,%s,%s)", (uuid4(), uuid4(), principal), "23503"),
            ("null-membership-principal", "INSERT INTO {s}.memberships(tenant_id,id,principal_id) VALUES (%s,%s,NULL)", (tenant, uuid4()), "23502"),
            ("invalid-access-revision", "UPDATE {s}.memberships SET access_revision=0 WHERE tenant_id=%s", (tenant,), "23514"),
            ("invalid-mutable-version", "UPDATE {s}.memberships SET version=0 WHERE tenant_id=%s", (tenant,), "23514"),
            ("unknown-timezone", "UPDATE {s}.tenants SET timezone='Not/AZone' WHERE id=%s", (tenant,), "23503"),
            ("duplicate-identity", "INSERT INTO {s}.principals(id,issuer,subject) VALUES (%s,%s,%s)", (uuid4(), "https://identity.example.test", str(principal)), "23505"),
            ("duplicate-tenant-slug", "UPDATE {s}.tenants SET slug='fixture-a' WHERE id=%s", (second,), "23505"),
            ("session-wrong-membership", "INSERT INTO {s}.sessions(id,principal_id,token_hash,csrf_hash,active_tenant_id,expires_at) VALUES (%s,%s,'other-hash','csrf',%s,CURRENT_TIMESTAMP+interval '1 hour')", (uuid4(), principal, second), "23503"),
            ("duplicate-session-hash", "INSERT INTO {s}.sessions(id,principal_id,token_hash,csrf_hash,expires_at) VALUES (%s,%s,'fixture-token-hash','csrf',CURRENT_TIMESTAMP+interval '1 hour')", (uuid4(), principal), "23505"),
            ("invalid-session-expiry", "INSERT INTO {s}.sessions(id,principal_id,token_hash,csrf_hash,expires_at) VALUES (%s,%s,'new-hash','csrf',CURRENT_TIMESTAMP-interval '1 hour')", (uuid4(), principal), "23514"),
            ("immutable-snapshot-update", "UPDATE {s}.tenant_configs SET assistant_name='Changed' WHERE tenant_id=%s", (tenant,), "55000"),
            ("immutable-snapshot-delete", "DELETE FROM {s}.tenant_configs WHERE tenant_id=%s", (tenant,), "55000"),
            ("immutable-snapshot-truncate", "TRUNCATE {s}.tenant_configs CASCADE", None, "55000"),
            ("immutable-policy-truncate", "TRUNCATE {s}.security_policy_configs", None, "55000"),
            ("duplicate-config-version", "INSERT INTO {s}.tenant_configs SELECT tenant_id,%s,config_version,assistant_name,locales,help_contact,instruction_text,connector_ref,model_policy_ref,schema_version,created_at FROM {s}.tenant_configs WHERE tenant_id=%s", (uuid4(), tenant), "23505"),
            ("invalid-locales", "INSERT INTO {s}.tenant_configs SELECT tenant_id,%s,3,assistant_name,%s,help_contact,instruction_text,connector_ref,model_policy_ref,schema_version,created_at FROM {s}.tenant_configs WHERE tenant_id=%s", (uuid4(), Jsonb(["fr"]), tenant), "23514"),
            ("invalid-json-schema-version", "INSERT INTO {s}.tenant_configs SELECT tenant_id,%s,3,assistant_name,locales,help_contact,instruction_text,connector_ref,model_policy_ref,2,created_at FROM {s}.tenant_configs WHERE tenant_id=%s", (uuid4(), tenant), "23514"),
            ("invalid-processing-policy", "INSERT INTO {s}.security_policy_configs(tenant_id,id,revision,category,processing_policy,schema_version) VALUES (%s,%s,1,'documents',%s,1)", (tenant, uuid4(), Jsonb([])), "23514"),
            ("invalid-retention", "INSERT INTO {s}.security_policy_configs(tenant_id,id,revision,category,processing_policy,schema_version,retention_seconds) VALUES (%s,%s,1,'documents',%s,1,0)", (tenant, uuid4(), Jsonb({})), "23514"),
            ("incomplete-policy-approval", "INSERT INTO {s}.security_policy_configs(tenant_id,id,revision,category,processing_policy,schema_version,approved_by,approved_at) VALUES (%s,%s,1,'documents',%s,1,%s,CURRENT_TIMESTAMP)", (tenant, uuid4(), Jsonb({}), principal), "23514"),
        ]
        for stage, statement, params, state in cases:
            await rejected(schema, stage, statement, params, state)

        stage = "session-before-tenant-selection-and-combined-roles"
        async with direct_connection(target) as connection:
            await connection.execute(query("INSERT INTO {s}.sessions(id,principal_id,token_hash,csrf_hash,expires_at) "
                "VALUES (%s,%s,'unselected-hash','csrf',CURRENT_TIMESTAMP+interval '1 hour')", schema), (uuid4(), principal))
            await connection.execute(query("INSERT INTO {s}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) "
                "VALUES (%s,%s,%s,'approver','tenant','self',%s)", schema), (tenant, uuid4(), membership, other))
        completed.append(stage)

        stage = "new-config-atomic-pointer"
        async with direct_connection(target) as connection:
            async with bounded_transaction(connection):
                await connection.execute(query("INSERT INTO {s}.tenant_configs SELECT tenant_id,%s,2,assistant_name,locales,help_contact,"
                    "instruction_text,connector_ref,model_policy_ref,schema_version,created_at FROM {s}.tenant_configs WHERE tenant_id=%s", schema), (uuid4(), tenant))
                await connection.execute(query("UPDATE {s}.tenants SET config_version=2,version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=%s", schema), (tenant,))
            await connection.execute(query("INSERT INTO {s}.security_policy_configs(tenant_id,id,revision,category,processing_policy,schema_version) VALUES (%s,%s,1,'documents',%s,1)", schema), (tenant, uuid4(), Jsonb({})))
        completed.append(stage)
        await rejected(schema, "immutable-security-policy", "UPDATE {s}.security_policy_configs SET region='changed' WHERE tenant_id=%s", (tenant,), "55000")

        stage = "checksum-drift"
        async with direct_connection(target) as connection:
            await connection.execute(query("UPDATE {s}.schema_migrations SET checksum=%s WHERE version=1", schema), ("0" * 64,))
        await expect_migration_failure(schema)
        async with direct_connection(target) as connection:
            await connection.execute(query("UPDATE {s}.schema_migrations SET checksum=%s WHERE version=1", schema), (revisions()[0].checksum,))
        completed.append(stage)

        stage = "failed-upgrade-rollback"
        bad = (*revisions(), Revision(head + 1, f"{head + 1:04d}_failure.sql", "CREATE TABLE {schema}.must_rollback (id integer); SELECT 1 / 0;"))
        await expect_migration_failure(schema, bad)
        require(await migrate(target, schema=schema) == (head, head), stage)
        async with direct_connection(target) as connection:
            cursor = await connection.execute("SELECT to_regclass(%s)", (schema + ".must_rollback",))
            require(await cursor.fetchone() == (None,), stage)
        completed.append(stage)

        stage = "failed-initial-migration-rollback"
        empty = await new_schema()
        await expect_migration_failure(empty, bad)
        require(await migrate(target, schema=empty) == (0, head), stage)
        completed.append(stage)

        stage = "unmanaged-schema-empty-ledger"
        unmanaged = await new_schema()
        async with direct_connection(target) as connection:
            async with bounded_transaction(connection):
                await connection.execute(query("CREATE SCHEMA {s}", unmanaged))
                await connection.execute(query("CREATE TABLE {s}.foreign_data (id integer)", unmanaged))
                await connection.execute(query("INSERT INTO {s}.foreign_data VALUES (42)", unmanaged))
                await connection.execute(query("CREATE TABLE {s}.schema_migrations (version integer, name text, checksum text)", unmanaged))
        await expect_migration_failure(unmanaged)
        async with direct_connection(target) as connection:
            cursor = await connection.execute("SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
                "ON c.relnamespace=n.oid WHERE n.nspname=%s AND c.relkind='r'", (unmanaged,))
            require(await cursor.fetchone() == (2,), stage)
            require(await (await connection.execute(query("SELECT id FROM {s}.foreign_data", unmanaged))).fetchall() == [(42,)], stage)
            require(await (await connection.execute(query("SELECT count(*) FROM {s}.schema_migrations", unmanaged))).fetchone() == (0,), stage)
        completed.append(stage)

        stage = "concurrent-empty-to-head"
        concurrent = await new_schema()
        async with asyncio.TaskGroup() as group:
            first = group.create_task(migrate(target, schema=concurrent, upgrade=True))
            second_run = group.create_task(migrate(target, schema=concurrent, upgrade=True))
        require(first.result() == second_run.result() == (head, head), stage)
        completed.append(stage)
    except VerificationFailure:
        raise
    except Exception:
        raise VerificationFailure(stage) from None
    finally:
        cleanup_failed = False
        for name in owned:
            try:
                await drop_probe_schema(target, name)
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            raise VerificationFailure("probe-cleanup") from None
    return len(completed)
