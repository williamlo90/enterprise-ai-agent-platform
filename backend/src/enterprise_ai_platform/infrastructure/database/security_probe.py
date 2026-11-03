"""Real login + RLS/transaction checks using disposable fictional tenants."""

import asyncio
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from enterprise_ai_platform.infrastructure.database.connection import (
    DatabaseLimits, DatabaseUnavailable, RuntimeDatabase, bounded_transaction, direct_connection, finish_cleanup,
)
from enterprise_ai_platform.infrastructure.database.migration import access_role, migrate
from enterprise_ai_platform.infrastructure.database.roles import RoleProvisioningError, drop_probe_schema, provision_role
from enterprise_ai_platform.modules.identity.domain import TenantActorContext


class SecurityVerificationFailure(RuntimeError):
    def __init__(self, case):
        super().__init__("Runtime isolation verification failed: " + case + ".")


async def verify_security(direct, base):
    schema = "db03_probe_" + uuid4().hex
    login = "eai_runtime_" + schema.rsplit("_", 1)[1]
    stage = "namespace-unused"
    checks = []
    owned = False
    database = None

    def query(source):
        return sql.SQL(source).format(s=sql.Identifier(schema))

    def require(condition):
        if not condition:
            raise SecurityVerificationFailure(stage)

    async def denied(statement, context=None, params=None):
        try:
            async with database.transaction(context) as connection:
                await connection.execute(statement, params)
        except DatabaseUnavailable:
            checks.append(stage)
        else:
            raise SecurityVerificationFailure(stage)

    async def rows(context=None, statement=None):
        async with database.transaction(context) as connection:
            return await (await connection.execute(statement or query("SELECT tenant_id FROM {s}._db03_records"))).fetchall()

    try:
        async with direct_connection(direct) as connection:
            require(await (await connection.execute("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=%s", (schema,))).fetchone() is None)
            require(await (await connection.execute("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname IN (%s,%s)", (access_role(schema), login))).fetchone() is None)
        owned = True
        stage = "migration-and-runtime-login"
        await migrate(direct, upgrade=True, schema=schema)
        target = await provision_role(direct, base, schema=schema, role_name=login)
        database = RuntimeDatabase(target, schema=schema, max_size=1)
        await database.open()
        checks.append(stage)
        actors = [uuid4(), uuid4()]
        tenants = [uuid4(), uuid4()]
        contexts = [TenantActorContext(tenants[i], actors[i]) for i in range(2)]
        stage = "fictional-fixture"
        async with direct_connection(direct) as connection:
            async with bounded_transaction(connection):
                for actor in actors:
                    await connection.execute(query("INSERT INTO {s}.principals(id,issuer,subject) VALUES (%s,'https://id.example.test',%s)"), (actor, str(actor)))
                for i in range(2):
                    await connection.execute(query("INSERT INTO {s}.tenants(id,slug,name,timezone) VALUES (%s,%s,'Fictional tenant','UTC')"), (tenants[i], "fixture-" + str(i)))
                    await connection.execute(query("INSERT INTO {s}.tenant_configs(tenant_id,id,config_version,assistant_name,locales,help_contact,"
                        "instruction_text,connector_ref,model_policy_ref,schema_version) VALUES (%s,%s,1,'Fixture',%s,'Administrator','Approved sources','fixture','fixture',1)"),
                        (tenants[i], uuid4(), Jsonb(["id", "en"])))
                    member = uuid4()
                    await connection.execute(query("INSERT INTO {s}.memberships(tenant_id,id,principal_id) VALUES (%s,%s,%s)"), (tenants[i], member, actors[i]))
                    await connection.execute(query("INSERT INTO {s}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) "
                        "VALUES (%s,%s,%s,'employee','tenant','self',%s)"), (tenants[i], uuid4(), member, actors[1-i]))
                # This temporary fixture models future tenant/owner feature writes.
                # Foundation writes remain ungranted in production and this test.
                await connection.execute(query("CREATE TABLE {s}._db03_records(tenant_id uuid NOT NULL,id uuid PRIMARY KEY,owner_id uuid NOT NULL)"))
                await connection.execute(query("ALTER TABLE {s}._db03_records ENABLE ROW LEVEL SECURITY"))
                await connection.execute(query("ALTER TABLE {s}._db03_records FORCE ROW LEVEL SECURITY"))
                await connection.execute(query("CREATE POLICY maintenance ON {s}._db03_records TO CURRENT_USER USING (true) WITH CHECK (true)"))
                await connection.execute(sql.SQL("CREATE POLICY tenant_actor ON {}._db03_records TO {} USING "
                    "({}.has_active_context(tenant_id) AND owner_id={}.context_actor_id()) WITH CHECK "
                    "({}.has_active_context(tenant_id) AND owner_id={}.context_actor_id())").format(
                        sql.Identifier(schema), sql.Identifier(access_role(schema)), *[sql.Identifier(schema)] * 4))
                await connection.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON {}._db03_records TO {}").format(
                    sql.Identifier(schema), sql.Identifier(access_role(schema))))

        stage = "same-tenant-writes"
        for i, context in enumerate(contexts):
            async with database.transaction(context) as connection:
                await connection.execute(query("INSERT INTO {s}._db03_records VALUES (%s,%s,%s)"), (tenants[i], uuid4(), actors[i]))
            require(await rows(context) == [(tenants[i],)])
        checks.append(stage)
        stage = "managed-provision-idempotent-password-preserved"
        existing = await provision_role(direct, target, schema=schema, role_name=login)
        require(existing == target)
        checks.append(stage)

        for recipient in (login, access_role(schema)):
            for privilege, table in (("UPDATE(role)", "membership_roles"), ("SELECT(token_hash)", "sessions"),
                                     ("SELECT(role)", "membership_roles")):
                stage = "column-privilege-startup-and-rotation-preflight"
                grant_option = privilege == "SELECT(role)"
                async with direct_connection(direct) as connection:
                    await connection.execute(sql.SQL("GRANT " + privilege + " ON {}.{} TO {}" +
                        (" WITH GRANT OPTION" if grant_option else "")).format(
                        sql.Identifier(schema), sql.Identifier(table), sql.Identifier(recipient)))
                unsafe_database = RuntimeDatabase(target, schema=schema)
                try:
                    try:
                        await unsafe_database.open()
                    except DatabaseUnavailable:
                        pass
                    else:
                        raise SecurityVerificationFailure(stage)
                    try:
                        await provision_role(direct, target, schema=schema, role_name=login, rotate=True)
                    except RoleProvisioningError:
                        pass
                    else:
                        raise SecurityVerificationFailure(stage)
                    # A failed preflight must leave the previous password usable.
                    async with direct_connection(target) as connection:
                        require(await (await connection.execute("SELECT 1")).fetchone() == (1,))
                    if privilege == "UPDATE(role)":
                        async with database.transaction(contexts[0]) as connection:
                            cursor = await connection.execute(query("UPDATE {s}.membership_roles SET role='tenant_admin'"))
                            require(cursor.rowcount == 0)  # Read-only RLS policies, even with a bad ACL.
                finally:
                    await unsafe_database.close()
                    async with direct_connection(direct) as connection:
                        await connection.execute(sql.SQL("REVOKE " + privilege + " ON {}.{} FROM {}").format(
                            sql.Identifier(schema), sql.Identifier(table), sql.Identifier(recipient)))
                checks.append(stage)


        for stage, context in (("missing-context", None), ("wrong-actor", TenantActorContext(tenants[0], actors[1])),
                               ("unknown-actor", TenantActorContext(tenants[0], uuid4())),
                               ("unknown-tenant", TenantActorContext(uuid4(), actors[0]))):
            require(await rows(context) == [])
            checks.append(stage)
        for i, context in enumerate(contexts):
            for stage, statement, expected in (
                ("self-principal", query("SELECT id FROM {s}.principals"), [(actors[i],)]),
                ("own-membership", query("SELECT principal_id FROM {s}.memberships"), [(actors[i],)]),
                ("own-role", query("SELECT tenant_id FROM {s}.membership_roles"), [(tenants[i],)]),
                ("tenant-header", query("SELECT id FROM {s}.tenants"), [(tenants[i],)]),
                ("tenant-config", query("SELECT tenant_id FROM {s}.tenant_configs"), [(tenants[i],)]),
            ):
                require(await rows(context, statement) == expected)
                checks.append(stage)

        stage = "concurrent-contexts-on-reused-pool"
        concurrent_contexts = [contexts[0], contexts[1], contexts[0], contexts[1]]
        results = await asyncio.gather(*(rows(context) for context in concurrent_contexts))
        require(results == [[(context.tenant_id,)] for context in concurrent_contexts])
        checks.append(stage)

        for label, tenant_value, actor_value in (
            ("invalid-tenant-context", "invalid", str(actors[0])),
            ("invalid-actor-context", str(tenants[0]), "invalid"),
            ("partial-tenant-context", str(tenants[0]), ""),
            ("partial-actor-context", "", str(actors[0])),
        ):
            stage = label
            async with database.transaction() as connection:
                await connection.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.actor_id',%s,true)", (tenant_value, actor_value))
                require(await (await connection.execute(query("SELECT id FROM {s}._db03_records"))).fetchall() == [])
            checks.append(stage)

        stage = "cross-tenant-insert"
        await denied(query("INSERT INTO {s}._db03_records VALUES (%s,%s,%s)"), contexts[0], (tenants[1], uuid4(), actors[1]))
        stage = "missing-context-insert"
        await denied(query("INSERT INTO {s}._db03_records VALUES (%s,%s,%s)"), None, (tenants[0], uuid4(), actors[0]))
        stage = "cross-tenant-update-check"
        await denied(query("UPDATE {s}._db03_records SET tenant_id=%s"), contexts[0], (tenants[1],))
        stage = "cross-tenant-delete-hidden"
        async with database.transaction(contexts[0]) as connection:
            cursor = await connection.execute(query("DELETE FROM {s}._db03_records WHERE tenant_id=%s"), (tenants[1],))
            require(cursor.rowcount == 0)
        require(await rows(contexts[1]) == [(tenants[1],)])
        checks.append(stage)

        for table in ("sessions", "security_policy_configs", "schema_migrations", "runtime_role_registry", "runtime_access_role_registry"):
            stage = "private-table-" + table
            await denied(sql.SQL("SELECT * FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table)), contexts[0])
        for stage, statement in (
            ("foundation-role-write", query("UPDATE {s}.membership_roles SET role='tenant_admin'")),
            ("foundation-member-write", query("UPDATE {s}.memberships SET active=false")),
            ("truncate-denied", query("TRUNCATE {s}._db03_records")),
            ("alter-policy-denied", query("ALTER TABLE {s}.tenants DISABLE ROW LEVEL SECURITY")),
            ("replace-helper-denied", query("CREATE OR REPLACE FUNCTION {s}.has_active_context(uuid) RETURNS boolean LANGUAGE sql AS 'SELECT true'")),
            ("set-access-role-denied", sql.SQL("SET ROLE {}").format(sql.Identifier(access_role(schema)))),
            ("set-owner-role-denied", sql.SQL("SET ROLE {}").format(sql.Identifier(direct.user))),
        ):
            await denied(statement, contexts[0])

        for table, key in (("memberships", "principal_id"), ("principals", "id"), ("tenants", "id")):
            stage = "inactive-" + table
            selected = actors[0] if table != "tenants" else tenants[0]
            async with direct_connection(direct) as connection:
                await connection.execute(sql.SQL("UPDATE {}.{} SET active=false WHERE {}=%s").format(
                    sql.Identifier(schema), sql.Identifier(table), sql.Identifier(key)), (selected,))
            require(await rows(contexts[0]) == [])
            async with direct_connection(direct) as connection:
                await connection.execute(sql.SQL("UPDATE {}.{} SET active=true WHERE {}=%s").format(
                    sql.Identifier(schema), sql.Identifier(table), sql.Identifier(key)), (selected,))
            checks.append(stage)

        stage = "success-and-business-error-pool-reuse"
        async with database.transaction(contexts[0]) as connection:
            first_connection = connection
        business_id = uuid4()
        try:
            async with database.transaction(contexts[0]) as connection:
                require(connection is first_connection)
                await connection.execute(query("INSERT INTO {s}._db03_records VALUES (%s,%s,%s)"), (tenants[0], business_id, actors[0]))
                raise ValueError("Synthetic business rollback")
        except ValueError:
            pass
        async with database.transaction() as connection:
            require(connection is first_connection)
            require(await (await connection.execute(query("SELECT id FROM {s}._db03_records"))).fetchall() == [])
        require(await rows(contexts[1]) == [(tenants[1],)])
        checks.append(stage)
        stage = "sql-error-pool-reuse"
        await denied("SELECT 1/0", contexts[0])
        require(await rows() == [] and await rows(contexts[1]) == [(tenants[1],)])

        rollback_ids = [business_id]
        for kind in ("cancel", "deadline", "statement-timeout"):
            stage = kind + "-cleanup-and-reuse"
            pending_id = uuid4()
            rollback_ids.append(pending_id)
            entered = asyncio.Event()
            previous = database.limits
            database.limits = DatabaseLimits(transaction_seconds=2, statement_ms=50 if kind == "statement-timeout" else 5000)
            async def work():
                async with database.transaction(contexts[0]) as connection:
                    await connection.execute(query("INSERT INTO {s}._db03_records VALUES (%s,%s,%s)"), (tenants[0], pending_id, actors[0]))
                    entered.set()
                    if kind == "statement-timeout":
                        await connection.execute("SELECT pg_sleep(0.2)")
                    else:
                        await asyncio.sleep(5)
            task = asyncio.create_task(work())
            try:
                if kind == "cancel":
                    async with asyncio.timeout(10):
                        await entered.wait()
                    task.cancel()
                    await asyncio.sleep(0.005)
                    task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, DatabaseUnavailable):
                    pass
                else:
                    raise SecurityVerificationFailure(stage)
            finally:
                database.limits = previous
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, DatabaseUnavailable):
                        pass
            require(await rows() == [] and await rows(contexts[1]) == [(tenants[1],)])
            checks.append(stage)
        stage = "rollback-data-absent"
        async with direct_connection(direct) as connection:
            require(await (await connection.execute(query("SELECT count(*) FROM {s}._db03_records WHERE id=ANY(%s)"), (rollback_ids,))).fetchone() == (0,))
        checks.append(stage)

        stage = "privileged-runtime-refused"
        owner_database = RuntimeDatabase(direct, schema=schema)
        try:
            try:
                await owner_database.open()
            except DatabaseUnavailable:
                pass
            else:
                raise SecurityVerificationFailure(stage)
        finally:
            await owner_database.close()
        checks.append(stage)

        stage = "startup-rejects-effective-extra-grant"
        async with direct_connection(direct) as connection:
            await connection.execute(sql.SQL("GRANT SELECT ON {}.sessions TO {}").format(sql.Identifier(schema), sql.Identifier(login)))
        extra_database = RuntimeDatabase(target, schema=schema)
        try:
            try:
                await extra_database.open()
            except DatabaseUnavailable:
                pass
            else:
                raise SecurityVerificationFailure(stage)
        finally:
            await extra_database.close()
            async with direct_connection(direct) as connection:
                await connection.execute(sql.SQL("REVOKE SELECT ON {}.sessions FROM {}").format(sql.Identifier(schema), sql.Identifier(login)))
        checks.append(stage)
    except SecurityVerificationFailure:
        raise
    except Exception:
        raise SecurityVerificationFailure(stage) from None
    finally:
        async def cleanup():
            try:
                if database is not None:
                    await database.close()
            finally:
                if owned:
                    await drop_probe_schema(direct, schema)
        await finish_cleanup(cleanup())
    return len(checks)
