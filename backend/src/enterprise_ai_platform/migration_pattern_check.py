"""DB-04 filled feature/rollout checks. Synthetic revisions never enter platform history."""

from contextlib import asynccontextmanager
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4
import psycopg
from psycopg import sql
from enterprise_ai_platform.authorization_check import Harness as Seed,CheckFailure,require
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase,direct_connection,bounded_transaction,parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import migrate,revisions,Revision,MigrationError,access_role
from enterprise_ai_platform.infrastructure.database.migration_patterns import pattern_source,backfill_batch
from enterprise_ai_platform.infrastructure.database.roles import provision_role,drop_probe_schema
from enterprise_ai_platform.integrations.identity.cognito_probe import safe_transport_logs
from enterprise_ai_platform.modules.identity.domain import TenantActorContext


class ObservedDatabase(RuntimeDatabase):
    last_sqlstate=None
    @asynccontextmanager
    async def _bounded_transaction(self,connection):
        try:
            async with super()._bounded_transaction(connection):yield
        except psycopg.Error as error:
            self.last_sqlstate=error.sqlstate
            raise


class Harness(Seed):
    def __init__(self,admin,database,schema,direct,runtime_name):
        super().__init__(admin,database,schema)
        self.direct,self.runtime_name=direct,runtime_name
        self.base=revisions();self.head=len(self.base);self.feature="db04_fixture"
        self.create=Revision(self.head+1,f"{self.head+1:04d}_feature.sql",pattern_source("create",self.feature))
        self.expand=Revision(self.head+2,f"{self.head+2:04d}_expand.sql",pattern_source("expand",self.feature))
        self.foundation=(*self.base,self.create,self.expand)
        self.a,self.b,self.actor,self.colleague=uuid4(),uuid4(),uuid4(),uuid4()
        self.parent_a,self.parent_c,self.parent_b=uuid4(),uuid4(),uuid4()
        self.entry_a,self.entry_c,self.entry_b=uuid4(),uuid4(),uuid4()
        self.checks=[]

    def passed(self,name):
        self.checks.append(name)
        if len(self.checks)%10==0:print("Feature migration checks: "+str(len(self.checks)),flush=True)

    async def apply(self,items,target=None):
        return await migrate(self.direct,schema=self.schema,items=items,upgrade=True,target_version=target)

    async def count(self,query,params=None):return (await (await self.execute(query,params)).fetchone())[0]

    async def setup(self):
        await self.member(self.a,self.actor);await self.member(self.a,self.colleague);await self.member(self.b,self.actor)
        await self.apply(self.foundation,self.head+1)
        async with self.admin.transaction():
            for tenant,owner,parent,entry,label in ((self.a,self.actor,self.parent_a,self.entry_a,"A-original"),
                    (self.a,self.colleague,self.parent_c,self.entry_c,"C-original"),(self.b,self.actor,self.parent_b,self.entry_b,"B-original")):
                await self.execute("INSERT INTO {s}.db04_fixture_collections(tenant_id,id,owner_id,current_entry_id) VALUES(%s,%s,%s,%s)",(tenant,parent,owner,entry))
                await self.execute("INSERT INTO {s}.db04_fixture_entries(tenant_id,id,parent_id,label) VALUES(%s,%s,%s,%s)",(tenant,entry,parent,label))
            for i in range(8):
                await self.execute("INSERT INTO {s}.db04_fixture_entries(tenant_id,id,parent_id,label) VALUES(%s,%s,%s,%s)",(self.a,uuid4(),self.parent_a,"historical-"+str(i)))
        self.passed("filled_foundation_to_feature_create")

    async def runtime(self,context,query,params=None):
        async with self.database.transaction(context) as connection:
            cursor=await connection.execute(sql.SQL(query).format(s=sql.Identifier(self.schema)),params)
            return await cursor.fetchall()

    async def rejected(self,name,context,query,params,state):
        observed=None
        try:
            async with self.database.transaction(context) as connection:
                try:await connection.execute(sql.SQL(query).format(s=sql.Identifier(self.schema)),params)
                except psycopg.Error as error:observed=error.sqlstate;raise
        except Exception:
            require(observed==state,name+"-sqlstate")
        else:raise CheckFailure(name+"-not-rejected")
        self.passed(name)

    async def cyclic_rejected(self,name,context,tenant,owner,parent,entry,*,pointer=None,column="label"):
        inserted=False;reached_end=False;self.database.last_sqlstate=None
        try:
            async with self.database.transaction(context) as connection:
                try:
                    await connection.execute(sql.SQL("INSERT INTO {s}.db04_fixture_collections(tenant_id,id,owner_id,current_entry_id) VALUES(%s,%s,%s,%s)").format(s=sql.Identifier(self.schema)),(tenant,parent,owner,pointer or entry))
                    inserted=True
                    if pointer is not None:
                        await connection.execute(sql.SQL("INSERT INTO {s}.db04_fixture_entries(tenant_id,id,parent_id,{column}) VALUES(%s,%s,%s,'New entry')").format(s=sql.Identifier(self.schema),column=sql.Identifier(column)),(tenant,entry,parent))
                    reached_end=True
                except psycopg.Error as error:state=error.sqlstate;raise
        except Exception:require(inserted and reached_end and self.database.last_sqlstate=="23503",name+"-deferred-at-commit")
        else:raise CheckFailure(name+"-not-rejected")
        require(await self.count("SELECT count(*) FROM {s}.db04_fixture_collections WHERE tenant_id=%s AND id=%s",(tenant,parent))==0,name+"-rollback")
        self.passed(name)

    async def boundaries(self,phase,column):
        a=TenantActorContext(self.a,self.actor);b=TenantActorContext(self.b,self.actor);c=TenantActorContext(self.a,self.colleague)
        for context,tenant,owner in ((a,self.a,self.actor),(b,self.b,self.actor),(c,self.a,self.colleague)):
            actual=await self.runtime(context,"SELECT e.id FROM {s}.db04_fixture_entries e ORDER BY e.id")
            expected=await (await self.execute("SELECT e.id FROM {s}.db04_fixture_entries e JOIN {s}.db04_fixture_collections p ON p.tenant_id=e.tenant_id AND p.id=e.parent_id WHERE p.tenant_id=%s AND p.owner_id=%s ORDER BY e.id",(tenant,owner))).fetchall()
            require(actual==expected,phase+"-owned-rows")
        require(await self.runtime(None,"SELECT count(*) FROM {s}.db04_fixture_entries")==[(0,)],phase+"-missing-context")
        self.passed(phase+"_rls_read_owner_tenant_and_missing_context")
        hidden_before=await (await self.execute("SELECT id,"+column+" FROM {s}.db04_fixture_entries WHERE id IN (%s,%s) ORDER BY id",(self.entry_c,self.entry_b))).fetchall()
        require(await self.runtime(a,"UPDATE {s}.db04_fixture_entries SET "+column+"='Denied update' WHERE id IN (%s,%s) RETURNING id",(self.entry_c,self.entry_b))==[],phase+"-hidden-updates")
        require(await (await self.execute("SELECT id,"+column+" FROM {s}.db04_fixture_entries WHERE id IN (%s,%s) ORDER BY id",(self.entry_c,self.entry_b))).fetchall()==hidden_before,phase+"-hidden-unchanged")
        self.passed(phase+"_foreign_and_colleague_updates_affect_zero_rows")
        await self.rejected(phase+"_foreign_tenant_insert",a,"INSERT INTO {s}.db04_fixture_collections(tenant_id,id,owner_id,current_entry_id) VALUES(%s,%s,%s,%s)",(self.b,uuid4(),self.actor,uuid4()),"42501")
        await self.rejected(phase+"_colleague_owner_spoof",a,"INSERT INTO {s}.db04_fixture_collections(tenant_id,id,owner_id,current_entry_id) VALUES(%s,%s,%s,%s)",(self.a,uuid4(),self.colleague,uuid4()),"42501")
        await self.rejected(phase+"_foreign_parent_child_insert",a,"INSERT INTO {s}.db04_fixture_entries(tenant_id,id,parent_id,"+column+") VALUES(%s,%s,%s,'Denied')",(self.a,uuid4(),self.parent_c),"42501")
        await self.rejected(phase+"_owner_column_update_denied",a,"UPDATE {s}.db04_fixture_collections SET owner_id=%s WHERE id=%s",(self.colleague,self.parent_a),"42501")
        await self.cyclic_rejected(phase+"_missing_current_entry",a,self.a,self.actor,uuid4(),uuid4(),column=column)
        await self.cyclic_rejected(phase+"_same_tenant_wrong_parent_pointer",a,self.a,self.actor,uuid4(),uuid4(),pointer=self.entry_c,column=column)
        await self.cyclic_rejected(phase+"_foreign_tenant_pointer",a,self.a,self.actor,uuid4(),uuid4(),pointer=self.entry_b,column=column)
        new_parent,new_entry=uuid4(),uuid4()
        async with self.database.transaction(a) as connection:
            await connection.execute(sql.SQL("INSERT INTO {s}.db04_fixture_collections(tenant_id,id,owner_id,current_entry_id) VALUES(%s,%s,%s,%s)").format(s=sql.Identifier(self.schema)),(self.a,new_parent,self.actor,new_entry))
            await connection.execute(sql.SQL("INSERT INTO {s}.db04_fixture_entries(tenant_id,id,parent_id,{column}) VALUES(%s,%s,%s,'Valid cycle')").format(s=sql.Identifier(self.schema),column=sql.Identifier(column)),(self.a,new_entry,new_parent))
        replacement=uuid4()
        async with self.database.transaction(a) as connection:
            await connection.execute(sql.SQL("INSERT INTO {s}.db04_fixture_entries(tenant_id,id,parent_id,{column}) VALUES(%s,%s,%s,'Replacement')").format(s=sql.Identifier(self.schema),column=sql.Identifier(column)),(self.a,replacement,new_parent))
            await connection.execute(sql.SQL("UPDATE {s}.db04_fixture_collections SET current_entry_id=%s,version=version+1 WHERE tenant_id=%s AND id=%s").format(s=sql.Identifier(self.schema)),(replacement,self.a,new_parent))
        self.passed(phase+"_valid_cycle_and_atomic_pointer_replacement")

    async def snapshot(self):
        result=[]
        for table in ("db04_fixture_entries","db04_fixture_collections","schema_migrations"):
            rows=await (await self.admin.execute(sql.SQL("SELECT row_to_json(t)::text FROM {}.{} t ORDER BY row_to_json(t)::text").format(sql.Identifier(self.schema),sql.Identifier(table)))).fetchall()
            result.append(rows)
        result.append(await (await self.execute("SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,c.relacl::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s ORDER BY c.relname",(self.schema,))).fetchall())
        result.append(await (await self.execute("SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE connamespace=(SELECT oid FROM pg_namespace WHERE nspname=%s) ORDER BY conname",(self.schema,))).fetchall())
        result.append(await (await self.execute("SELECT tablename,indexdef FROM pg_indexes WHERE schemaname=%s ORDER BY tablename,indexname",(self.schema,))).fetchall())
        return hashlib.sha256(json.dumps(result,default=str,sort_keys=True).encode()).hexdigest()

    async def expect_failure(self,items,name):
        before=await self.snapshot()
        try:await self.apply(items)
        except MigrationError:pass
        else:raise CheckFailure(name+"-not-failed")
        require(await self.snapshot()==before,name+"-full-rollback")
        self.passed(name)

    async def rollout(self):
        async with asyncio.TaskGroup() as group:
            one=group.create_task(self.apply(self.foundation));two=group.create_task(self.apply(self.foundation))
        require(one.result()==two.result()==(self.head+2,self.head+2),"concurrent-expand")
        self.passed("concurrent_expand_applied_once")
        await self.boundaries("expanded","label")
        context=TenantActorContext(self.a,self.actor)
        legacy=await self.runtime(context,"SELECT id,label FROM {s}.db04_fixture_entries ORDER BY id")
        fallback=await self.runtime(context,"SELECT id,coalesce(display_label,label) FROM {s}.db04_fixture_entries ORDER BY id")
        require(legacy==fallback,"v2-fallback")
        self.passed("v1_reader_and_v2_fallback_on_populated_expand")
        await self.runtime(context,"UPDATE {s}.db04_fixture_entries SET label='Old writer',version=version+1 WHERE id=%s RETURNING id",(self.entry_a,))
        require(await self.count("SELECT count(*) FROM {s}.db04_fixture_entries WHERE id=%s AND display_label=label",(self.entry_a,))==1,"old-writer-sync")
        await self.runtime(context,"UPDATE {s}.db04_fixture_entries SET display_label='New writer',version=version+1 WHERE id=%s RETURNING id",(self.entry_a,))
        require(await self.count("SELECT count(*) FROM {s}.db04_fixture_entries WHERE id=%s AND label='New writer' AND display_label=label",(self.entry_a,))==1,"new-writer-sync")
        self.passed("old_and_new_writer_bidirectional_compatibility")
        await self.rejected("conflicting_dual_write_rejected",context,"UPDATE {s}.db04_fixture_entries SET label='One',display_label='Two' WHERE id=%s",(self.entry_a,),"23514")
        contract=Revision(self.head+3,f"{self.head+3:04d}_contract.sql",pattern_source("contract",self.feature,legacy_clients_retired=True))
        await self.expect_failure((*self.foundation,contract),"incomplete_backfill_contract_rolls_back")
        # Lock every pending row: SKIP LOCKED can legitimately produce zero.
        async with self.admin.transaction():
            await self.execute("SELECT id FROM {s}.db04_fixture_entries WHERE display_label IS NULL FOR UPDATE")
            require(await backfill_batch(self.direct,schema=self.schema,feature=self.feature,batch_size=2)==0,"locked-batch-zero")
            require(await self.count("SELECT count(*) FROM {s}.db04_fixture_entries WHERE display_label IS NULL")>0,"zero-not-completion")
        self.passed("zero_backfill_batch_is_not_completion")
        values=await (await self.execute("SELECT tenant_id,id,label FROM {s}.db04_fixture_entries ORDER BY tenant_id,id")).fetchall()
        count=await backfill_batch(self.direct,schema=self.schema,feature=self.feature,batch_size=2)
        require(0<count<=2,"bounded-batch")
        self.passed("bounded_backfill_batch_resumes_after_commit")
        async with asyncio.TaskGroup() as group:
            x=group.create_task(backfill_batch(self.direct,schema=self.schema,feature=self.feature,batch_size=2))
            y=group.create_task(backfill_batch(self.direct,schema=self.schema,feature=self.feature,batch_size=2))
        require(0<=x.result()<=2 and 0<=y.result()<=2,"concurrent-batches")
        for _ in range(30):
            if await self.count("SELECT count(*) FROM {s}.db04_fixture_entries WHERE display_label IS NULL")==0:break
            await backfill_batch(self.direct,schema=self.schema,feature=self.feature,batch_size=2)
        require(await self.count("SELECT count(*) FROM {s}.db04_fixture_entries WHERE display_label IS NULL OR display_label IS DISTINCT FROM label")==0,"backfill-complete")
        require(await (await self.execute("SELECT tenant_id,id,display_label FROM {s}.db04_fixture_entries ORDER BY tenant_id,id")).fetchall()==values,"backfill-preserved-data")
        require(await backfill_batch(self.direct,schema=self.schema,feature=self.feature,batch_size=2)==0,"backfill-idempotent")
        self.passed("concurrent_backfill_preserves_data_and_is_idempotent")
        # v1 binary rollback works while the expand schema is retained.
        await self.runtime(context,"UPDATE {s}.db04_fixture_entries SET label='Rollback v1',version=version+1 WHERE id=%s RETURNING id",(self.entry_a,))
        require(await self.runtime(context,"SELECT label FROM {s}.db04_fixture_entries WHERE id=%s",(self.entry_a,))==[("Rollback v1",)],"v1-rollback")
        self.passed("application_rollback_on_expand_schema")
        version=await self.count("SELECT version FROM {s}.db04_fixture_entries WHERE id=%s",(self.entry_a,))
        async def write(column,value):
            return await self.runtime(context,"UPDATE {s}.db04_fixture_entries SET "+column+"=%s,version=version+1 WHERE id=%s AND version=%s RETURNING id",(value,self.entry_a,version))
        winners=await asyncio.gather(write("label","v1 CAS"),write("display_label","v3 CAS"))
        require(sum(len(v) for v in winners)==1,"dual-writer-cas")
        require(await self.count("SELECT count(*) FROM {s}.db04_fixture_entries WHERE id=%s AND label=display_label",(self.entry_a,))==1,"dual-writer-consistency")
        self.passed("concurrent_old_new_writer_cas_single_winner")

    async def wait_for_sleep(self,task):
        for _ in range(150):
            if task.done():return False
            found=await self.count("SELECT count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND usename=current_user AND query LIKE %s AND wait_event='PgSleep'",("%"+self.schema+"%",))
            if found:return True
            await asyncio.sleep(0.02)
        return False

    async def failures(self):
        head=self.head+2
        changes="CREATE TABLE {schema}.rollback_marker(id integer); INSERT INTO {schema}.rollback_marker VALUES(1); "
        changes+="UPDATE {schema}.db04_fixture_entries SET label='Must rollback',version=version+1; "
        changes+="ALTER TABLE {schema}.db04_fixture_entries ADD CONSTRAINT failed_batch_check CHECK(version<1000000); "
        changes+="GRANT DELETE ON {schema}.db04_fixture_entries TO {access_role}; ALTER TABLE {schema}.db04_fixture_entries DISABLE ROW LEVEL SECURITY;"
        r9=Revision(head+1,f"{head+1:04d}_pending_change.sql",changes)
        broken=Revision(head+2,f"{head+2:04d}_pending_finish.sql","SELECT 1/0;")
        await self.expect_failure((*self.foundation,r9,broken),"later_revision_error_rolls_back_data_ddl_acl_rls_ledger")
        unsafe=Revision(head+1,f"{head+1:04d}_unsafe.sql",changes+" COMMIT; SELECT 1/0;")
        await self.expect_failure((*self.foundation,unsafe),"transaction_escape_preflight_preserves_full_state")
        lock_timeout=Revision(head+1,f"{head+1:04d}_locked.sql","SET LOCAL lock_timeout='100ms'; "+changes)
        async with self.admin.transaction():
            await self.execute("SELECT id FROM {s}.db04_fixture_entries WHERE id=%s FOR UPDATE",(self.entry_a,))
            await self.expect_failure((*self.foundation,lock_timeout),"lock_timeout_rolls_back_entire_pending_batch")
        # Both revision bodies finish; the deferred pointer fails at transaction exit.
        bad_parent,bad_child=uuid4(),uuid4()
        deferred=Revision(head+2,f"{head+2:04d}_pending_finish.sql",'INSERT INTO {schema}.db04_fixture_collections(tenant_id,id,owner_id,current_entry_id) VALUES(\''+str(self.a)+"','"+str(bad_parent)+"','"+str(self.actor)+"','"+str(bad_child)+"'); SELECT pg_sleep(1) FROM {schema}.rollback_marker;")
        before=await self.snapshot();task=asyncio.create_task(self.apply((*self.foundation,r9,deferred)))
        try:
            require(await self.wait_for_sleep(task),"deferred-end-reached")
            try:await task
            except MigrationError:pass
            else:raise CheckFailure("deferred-batch-did-not-fail")
        finally:
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        require(await self.snapshot()==before,"deferred-full-rollback")
        self.passed("deferred_commit_failure_rolls_back_entire_pending_batch")
        timeout=Revision(head+1,f"{head+1:04d}_timeout.sql",changes+" SET LOCAL statement_timeout='50ms'; SELECT pg_sleep(1) FROM {schema}.rollback_marker;")
        await self.expect_failure((*self.foundation,timeout),"statement_timeout_rolls_back_entire_batch")
        cancelled=Revision(head+1,f"{head+1:04d}_cancel.sql",changes+" SELECT pg_sleep(10) FROM {schema}.rollback_marker;")
        before=await self.snapshot();task=asyncio.create_task(self.apply((*self.foundation,cancelled)))
        try:
            require(await self.wait_for_sleep(task),"cancel-work-started")
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
            else:raise CheckFailure("migration-cancel-not-propagated")
        finally:
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        require(await self.snapshot()==before,"cancel-full-rollback")
        key=int.from_bytes(hashlib.sha256(("enterprise-ai:"+self.schema).encode()).digest()[:8],"big",signed=True)
        async with self.admin.transaction():
            require(await self.count("SELECT pg_try_advisory_xact_lock(%s)",(key,)),"migration-lock-released")
        self.passed("cancellation_rolls_back_and_releases_migration_lock")
        # A failed, unapplied revision can be corrected. Applied history stays immutable.
        safe=changes.split("GRANT DELETE",1)[0].replace("Must rollback","Corrected retry")
        good9=Revision(head+1,r9.name,safe);good10=Revision(head+2,broken.name,"SELECT 1;")
        self.final_items=(*self.foundation,good9,good10)
        require(await self.apply(self.final_items)==(head+2,head+2),"corrected-retry")
        self.passed("corrected_unapplied_batch_retries_successfully")

    async def contract(self):
        context=TenantActorContext(self.a,self.actor)
        await self.runtime(context,"UPDATE {s}.db04_fixture_entries SET display_label='v3 only',version=version+1 WHERE id=%s RETURNING id",(self.entry_a,))
        before=await self.runtime(context,"SELECT id,display_label FROM {s}.db04_fixture_entries ORDER BY id")
        target=len(self.final_items)+1
        contract=Revision(target,f"{target:04d}_contract.sql",pattern_source("contract",self.feature,legacy_clients_retired=True))
        items=(*self.final_items,contract)
        require(await self.apply(items)==(target,target),"contract-target")
        require(await self.runtime(context,"SELECT id,display_label FROM {s}.db04_fixture_entries ORDER BY id")==before,"v3-reader-after-contract")
        await self.runtime(context,"UPDATE {s}.db04_fixture_entries SET display_label='v3 after contract',version=version+1 WHERE id=%s RETURNING id",(self.entry_a,))
        self.passed("retired_legacy_clients_contract_preserves_v3_reader_writer")
        await self.rejected("legacy_reader_is_not_rollback_compatible_after_contract",context,"SELECT label FROM {s}.db04_fixture_entries",None,"42703")
        await self.rejected("new_required_column_remains_nonnull",context,"UPDATE {s}.db04_fixture_entries SET display_label=NULL WHERE id=%s",(self.entry_a,),"23502")
        await self.boundaries("contracted","display_label")
        require(await self.apply(items)==(target,target),"replay-contract")
        require(await self.database.ping(),"pool-health")
        self.passed("contract_replay_idempotent_and_runtime_pool_usable")


async def verify(direct,base):
    schema="db03_probe_"+uuid4().hex;database=None;owned=False;stage="namespace"
    try:
        before=await migrate(direct)
        async with direct_connection(direct) as connection:
            require((await (await connection.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s",(schema,))).fetchone()) is None,"namespace")
        owned=True;stage="foundation";await migrate(direct,schema=schema,upgrade=True)
        target=await provision_role(direct,base,schema=schema,role_name="eai_runtime_"+schema.rsplit("_",1)[1])
        database=ObservedDatabase(target,schema=schema);await database.open()
        async with direct_connection(direct,timeout=600) as connection:
            harness=Harness(connection,database,schema,direct,target.user)
            stage="filled-setup";await harness.setup()
            stage="base-boundaries";await harness.boundaries("base","label")
            stage="expand-backfill";await harness.rollout()
            stage="rollback-and-retry";await harness.failures()
            stage="contract";await harness.contract()
        require(await migrate(direct)==before,"platform-history-unchanged")
        return {"passed":True,"checks":harness.checks,"check_count":len(harness.checks),"platform_schema_modified":False,
                "platform_revision":before[0],"synthetic_feature_revisions_only":True,"real_postgres_restricted_runtime":True,"disposable_cleanup":"completed"}
    except Exception as error:
        raise CheckFailure(stage+":"+(str(error) if isinstance(error,CheckFailure) else type(error).__name__)) from None
    finally:
        if database is not None:await database.close()
        if owned:await drop_probe_schema(direct,schema)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--live",action="store_true")
    parser.add_argument("--env-file",type=Path,default=Path(".env"));parser.add_argument("--output",type=Path,default=Path("local/db04/feature-migrations-live.json"));args=parser.parse_args()
    if not args.live:parser.error("Explicit --live is required.")
    safe_transport_logs()
    try:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text('{"passed":false,"status":"running"}',encoding="utf-8")
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base))
        args.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8");print(json.dumps(result,sort_keys=True))
    except Exception as error:
        print("Feature migration verification failed: "+(str(error) if isinstance(error,CheckFailure) else "sanitized_operation_failure"),file=sys.stderr)
        raise SystemExit(1) from None


if __name__=="__main__":main()
