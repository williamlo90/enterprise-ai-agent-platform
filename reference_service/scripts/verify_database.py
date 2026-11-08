"""Opt-in serial REF-01 proof against owned namespaces on the existing test DB.

Never imports platform source. The operator DSN is read internally; output is
limited to case names, source hashes, and cleanup state. No credential hashes.
"""
import argparse
import asyncio
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
from urllib.parse import quote
from uuid import uuid4
import httpx
from psycopg import sql
from dotenv import dotenv_values
from reimbursement_service.config import Settings
from reimbursement_service.composition import database_loop_factory
from reimbursement_service.infrastructure.database.connection import RuntimeDatabase, Limits, direct_connection
from reimbursement_service.infrastructure.database import migration
from reimbursement_service.infrastructure.database.target import parse_target, schema_and_role
from reimbursement_service.main import create_app
from reimbursement_service.modules.system.domain import ActorContext, DatabaseUnavailable

SERVICE = Path(__file__).resolve().parents[1]
REPO = SERVICE.parent


class ProofFailure(RuntimeError):
    pass


def fingerprint():
    result={}
    for path in sorted(SERVICE.rglob('*')):
        rel=path.relative_to(SERVICE)
        if any(part in {'.venv','local','__pycache__','.pytest_cache','dist'} for part in rel.parts): continue
        if path.is_file() and (path.suffix in {'.py','.sql','.toml','.lock'}):
            result[rel.as_posix()]=sha256(path.read_bytes()).hexdigest()
    return result


async def verify(admin):
    schema='ref01_probe_'+uuid4().hex
    runtime=replace(admin,user=schema_and_role(schema),password=secrets.token_urlsafe(48))
    db=None; role_oid=None; schema_oid=None; checks=[]; stage='preflight'; cleanup=False
    prefix=lambda source: sql.SQL(source).format(s=sql.Identifier(schema),r=sql.Identifier(runtime.user))

    def require(value):
        if not value: raise ProofFailure(stage)

    async def operator(statement,params=None):
        async with direct_connection(admin) as connection:
            cursor=await connection.execute(statement,params)
            return await cursor.fetchall() if cursor.description else []

    async def records(context=None):
        async with db.transaction(context) as connection:
            return await (await connection.execute(prefix('SELECT tenant_id FROM {s}.tenant_registry ORDER BY tenant_id'))).fetchall()

    async def denied(statement,context=None):
        try:
            async with db.transaction(context) as connection: await connection.execute(statement)
        except DatabaseUnavailable: return
        raise ProofFailure(stage)

    try:
        require(not await operator('SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=%s',(schema,)))
        require(not await operator('SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=%s',(runtime.user,)))
        stage='empty_status_no_mutation'
        require(await migration.migrate(admin,runtime,schema=schema)==(0,len(migration.revisions())))
        require(not await operator('SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=%s',(schema,)))
        checks.append(stage)
        stage='atomic_migration_and_dedicated_login'
        require(await migration.migrate(admin,runtime,schema=schema,upgrade=True)==(len(migration.revisions()),len(migration.revisions())))
        role_oid=(await operator('SELECT oid FROM pg_catalog.pg_roles WHERE rolname=%s',(runtime.user,)))[0][0]
        schema_oid=(await operator('SELECT oid FROM pg_catalog.pg_namespace WHERE nspname=%s',(schema,)))[0][0]
        db=RuntimeDatabase(runtime,schema,max_size=1)
        await db.open()
        async with db.transaction() as connection:
            require(connection.pgconn.ssl_in_use)
            require((await (await connection.execute('SELECT session_user,current_user')).fetchone())==(runtime.user,runtime.user))
        checks.append(stage)
        stage='migration_replay_and_status_preserve_history'
        require(await migration.migrate(admin,runtime,schema=schema,upgrade=True)==(len(migration.revisions()),len(migration.revisions())))
        require(await migration.migrate(admin,runtime,schema=schema)==(len(migration.revisions()),len(migration.revisions())))
        checks.append(stage)
        stage='checksum_drift_rejected'
        checksum=migration.revisions()[0].checksum
        await operator(prefix("UPDATE {s}.schema_migrations SET checksum=repeat('0',64) WHERE version=1"))
        try:
            try: await migration.migrate(admin,runtime,schema=schema,upgrade=True)
            except migration.MigrationError: pass
            else: raise ProofFailure(stage)
        finally: await operator(prefix('UPDATE {s}.schema_migrations SET checksum=%s WHERE version=1'),(checksum,))
        checks.append(stage)
        tenants=[uuid4(),uuid4()]; actors=[uuid4(),uuid4()]
        contexts=[ActorContext(tenants[i],actors[i]) for i in range(2)]
        for tenant in tenants:
            await operator(prefix("INSERT INTO {s}.tenant_registry(tenant_id,provisioning_digest,active) VALUES (%s,%s,true)"),(tenant,'a'*64))
        stage='two_tenants_current_context_and_no_ambient_context'
        for i in range(2): require(await records(contexts[i])==[(tenants[i],)])
        require(await records()==[])
        require(await records(ActorContext(uuid4(),uuid4()))==[])
        checks.append(stage)
        stage='two_overlapping_requests_reuse_single_connection_safely'
        require(await asyncio.gather(records(contexts[0]),records(contexts[1]))==[[(tenants[0],)],[(tenants[1],)]])
        require(await records()==[])
        checks.append(stage)
        stage='malformed_and_partial_context_deny'
        for tenant,actor in [('invalid',str(actors[0])),(str(tenants[0]),''),(str(tenants[0]),'invalid')]:
            async with db.transaction() as connection:
                await connection.execute("SELECT set_config('reimbursement.tenant_id',%s,true),set_config('reimbursement.actor_id',%s,true)",(tenant,actor))
                require(await (await connection.execute(prefix('SELECT tenant_id FROM {s}.tenant_registry'))).fetchall()==[])
        checks.append(stage)
        stage='inactive_tenant_denied'
        await operator(prefix('UPDATE {s}.tenant_registry SET active=false WHERE tenant_id=%s'),(tenants[0],))
        require(await records(contexts[0])==[])
        await operator(prefix('UPDATE {s}.tenant_registry SET active=true WHERE tenant_id=%s'),(tenants[0],))
        checks.append(stage)
        stage='private_ledger_binding_and_mutation_denied'
        for statement in ['SELECT * FROM {s}.runtime_binding','SELECT * FROM {s}.schema_migrations',
                          'DELETE FROM {s}.tenant_registry','TRUNCATE {s}.tenant_registry',
                          'CREATE TABLE {s}.forbidden(id int)']:
            await denied(prefix(statement),contexts[0])
        checks.append(stage)
        stage='transaction_rollback_cleans_context'
        try:
            async with db.transaction(contexts[0]) as connection:
                await connection.execute("SELECT set_config('reimbursement.actor_id',%s,true)",(str(actors[1]),))
                raise ValueError('deliberate rollback')
        except ValueError: pass
        require(await records()==[] and await records(contexts[1])==[(tenants[1],)])
        checks.append(stage)
        stage='statement_timeout_discards_then_recovers'
        short=RuntimeDatabase(runtime,schema,max_size=1,limits=Limits(statement_ms=50))
        try:
            await short.open()
            try:
                async with short.transaction(contexts[0]) as connection: await connection.execute('SELECT pg_sleep(0.2)')
            except DatabaseUnavailable: pass
            else: raise ProofFailure(stage)
            require(await short.ping())
        finally: await short.close()
        checks.append(stage)
        stage='cancellation_releases_checkout'
        entered=asyncio.Event()
        async def work():
            async with db.transaction(contexts[0]): entered.set(); await asyncio.Event().wait()
        task=asyncio.create_task(work()); await entered.wait(); task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        require(await records()==[] and await db.ping())
        checks.append(stage)
        stage='column_privilege_drift_fails_closed'
        await operator(prefix('GRANT SELECT(role_name) ON {s}.runtime_binding TO {r}'))
        try: require(not await db.ping())
        finally: await operator(prefix('REVOKE SELECT(role_name) ON {s}.runtime_binding FROM {r}'))
        require(await db.ping()); checks.append(stage)
        stage='write_privilege_drift_fails_closed'
        await operator(prefix('GRANT UPDATE(active) ON {s}.tenant_registry TO {r}'))
        try: require(not await db.ping())
        finally: await operator(prefix('REVOKE UPDATE(active) ON {s}.tenant_registry FROM {r}'))
        require(await db.ping()); checks.append(stage)
        stage='rls_disabled_fails_closed'
        await operator(prefix('ALTER TABLE {s}.tenant_registry DISABLE ROW LEVEL SECURITY'))
        try: require(not await db.ping())
        finally: await operator(prefix('ALTER TABLE {s}.tenant_registry ENABLE ROW LEVEL SECURITY'))
        require(await db.ping()); checks.append(stage)
        stage='actual_api_lifecycle_readiness_and_business_absence'
        url=f'postgresql://{quote(runtime.user,safe="")}:{quote(runtime.password,safe="")}@{runtime.host}:{runtime.port}/{quote(runtime.dbname,safe="")}'
        app=create_app(Settings(database_url=url,schema=schema,pool_size=1))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://service.test') as client:
                require((await client.get('/health/ready')).status_code==200)
                require((await client.post('/service/v1/claims',json={'tenant_id':str(tenants[0])})).status_code==404)
                await operator(prefix('GRANT SELECT(role_name) ON {s}.runtime_binding TO {r}'))
                try:
                    response=await client.get('/health/ready')
                    require(response.status_code==503 and response.json()['error']['retryable'])
                    require((await client.get('/health/live')).status_code==200)
                finally: await operator(prefix('REVOKE SELECT(role_name) ON {s}.runtime_binding FROM {r}'))
        checks.append(stage)
        stage='failed_migration_rolls_back_schema_role_and_ledger'
        failing_schema='ref01_probe_'+uuid4().hex
        failing_runtime=replace(runtime,user=schema_and_role(failing_schema))
        original=migration.revisions
        migration.revisions=lambda: original()+(migration.Revision(len(original())+1,'9999_failure.sql','SELECT 1/0;'),)
        try:
            try: await migration.migrate(admin,failing_runtime,schema=failing_schema,upgrade=True)
            except migration.MigrationError: pass
            else: raise ProofFailure(stage)
        finally: migration.revisions=original
        require(not await operator('SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=%s',(failing_schema,)))
        require(not await operator('SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=%s',(failing_runtime.user,)))
        checks.append(stage)
    except Exception as exc:
        if type(exc) is ProofFailure: raise
        raise ProofFailure(stage+':'+type(exc).__name__) from None
    finally:
        if db is not None: await db.close()
        # The generated namespace is removable only with the recorded role/OID
        # binding. No broad DROP OWNED, platform mutation or user process cleanup.
        async with direct_connection(admin) as connection:
            namespace=await (await connection.execute('SELECT oid FROM pg_catalog.pg_namespace WHERE nspname=%s',(schema,))).fetchone()
            role=await (await connection.execute('SELECT oid FROM pg_catalog.pg_roles WHERE rolname=%s',(runtime.user,))).fetchone()
            if namespace is not None:
                binding=await (await connection.execute(prefix('SELECT role_name,role_oid FROM {s}.runtime_binding'))).fetchall()
                if binding!=[(runtime.user,role[0] if role else None)]: raise ProofFailure('cleanup_binding')
                if schema_oid is not None and namespace!=(schema_oid,): raise ProofFailure('cleanup_namespace')
                if role_oid is not None and role!=(role_oid,): raise ProofFailure('cleanup_role')
                await connection.execute(prefix('DROP SCHEMA {s} CASCADE'))
                dbname=(await (await connection.execute('SELECT current_database()')).fetchone())[0]
                await connection.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM {}').format(sql.Identifier(dbname),sql.Identifier(runtime.user)))
                await connection.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(runtime.user)))
            elif role is not None: raise ProofFailure('cleanup_unbound_role')
            remaining=await (await connection.execute(
                'SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=%s) OR EXISTS(SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=%s)',(schema,runtime.user))).fetchone()
            cleanup=remaining==(False,)
    return dict(status='PASS',checks=checks,cleanup=cleanup,fixture='isolated schema and dedicated service runtime login; existing managed test database',
                deployment=False,model_inference=False)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    before=fingerprint()
    platform={p.name:sha256(p.read_bytes()).hexdigest() for p in (REPO/'backend/src/enterprise_ai_platform/infrastructure/database/migrations').glob('*.sql')}
    try:
        env=dotenv_values(args.env_file)
        target=parse_target(env.get('DATABASE_DIRECT_URL') or os.environ.get('DATABASE_DIRECT_URL',''),direct=True)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            result=runner.run(verify(target))
        result['source_hashes']=before
        result['source_unchanged']=before==fingerprint()
        result['platform_migrations_unchanged']=platform=={p.name:sha256(p.read_bytes()).hexdigest() for p in (REPO/'backend/src/enterprise_ai_platform/infrastructure/database/migrations').glob('*.sql')}
        if not result['cleanup'] or not result['source_unchanged'] or not result['platform_migrations_unchanged']:
            raise ProofFailure('evidence_or_cleanup')
    except BaseException as exc:
        # Never emit a database/connection exception or traceback.
        label=str(exc) if type(exc) is ProofFailure else type(exc).__name__
        result={'status':'FAIL','case':label,'source_hashes':before}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print('REF-01 DB verification FAIL: '+label)
        raise SystemExit(1) from None
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(f"REF-01 DB verification PASS: {len(result['checks'])} cases; owned cleanup complete.")


if __name__=='__main__': main()
