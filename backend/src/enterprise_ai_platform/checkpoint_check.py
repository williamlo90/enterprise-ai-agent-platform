"""CHK-01 disposable PostgreSQL/LangGraph checks, fictional state, no models."""
import argparse
import asyncio
import json
import logging
import re
from pathlib import Path
from typing import TypedDict
from uuid import UUID,uuid4
from langgraph.graph import StateGraph,START,END
from langgraph.types import interrupt,Command
from enterprise_ai_platform.conversation_check import Harness as ConversationHarness
from enterprise_ai_platform.conversation_check import ControlledSources
from enterprise_ai_platform.file_check import Parser
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase,direct_connection,parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import migrate
from enterprise_ai_platform.infrastructure.database.roles import provision_role,drop_probe_schema
from enterprise_ai_platform.infrastructure.database.checkpoints import PostgresCheckpointRepository
from enterprise_ai_platform.integrations.checkpoints.langgraph import BoundCheckpointSaver
from enterprise_ai_platform.modules.conversations.ports import ConversationFailure,SourceRef
from enterprise_ai_platform.modules.identity.service import digest
from enterprise_ai_platform.modules.jobs.domain import Lease
from enterprise_ai_platform.knowledge_check import source_hashes,write_json
from psycopg import sql


class DiagnosticRepository(PostgresCheckpointRepository):
    async def execute(self,connection,operation,payload):
        try:return await super().execute(connection,operation,payload)
        except ConversationFailure as error:
            print('Fenced RPC rejected: '+operation+':'+str(error.status)+':'+str(getattr(error,'rejection_reason','')),flush=True);raise
        except Exception as error:
            print('RPC diagnostic: '+operation+':'+type(error).__name__+':'+str(getattr(error,'sqlstate',None))+':'+str(re.findall(r'line [0-9]+',str(getattr(getattr(error,'diag',None),'context','')))),flush=True);raise


class State(TypedDict):
    value:int
    accepted:bool


def graph(saver,twice=False):
    async def prepare(state):return {'value':state['value']+1}
    async def confirm(state):
        first=interrupt('Fictional human confirmation') is True
        second=interrupt('Fictional second confirmation') is True if twice else True
        return {'accepted':first and second}
    builder=StateGraph(State);builder.add_node('prepare',prepare);builder.add_node('confirm',confirm)
    builder.add_edge(START,'prepare');builder.add_edge('prepare','confirm');builder.add_edge('confirm',END)
    return builder.compile(checkpointer=saver)


class Harness(ConversationHarness):
    async def new_run(self):
        cid=(await self.create_conversation()).json()['id'];response=await self.append(cid)
        require(response.status_code==202,'admission');rid=response.json()['run_id']
        lease=await self.scheduler.claim();require(lease.get('id')==rid,'scheduler-claim')
        saver=BoundCheckpointSaver(DiagnosticRepository(self.database,self.sources),credential_hash=self.jobs._credential_hash,tenant_id=str(self.people[0]['tenant']),run_id=rid,job_id=lease['id'],generation=lease['generation'])
        await saver.start(model_ref='fictional-no-inference',prompt_version='chk01-v1',config_version=1)
        return saver

    async def rejected(self,call,status=None):
        try:await call
        except ConversationFailure as error:
            if status is not None:require(error.status==status,'rejection-status-'+str(error.status)+'-expected-'+str(status))
        else:raise CheckFailure('operation-unexpectedly-authorized')

    async def lifecycle(self):
        saver=await self.new_run();cfg=saver.config();g=graph(saver);print('Checkpoint lifecycle start accepted',flush=True)
        first=await g.ainvoke({'value':3,'accepted':False},cfg)
        require(first['value']==4 and first.get('__interrupt__'),'actual-interrupt')
        print('First interrupt persisted',flush=True)
        history=[v async for v in saver.alist(cfg,limit=20)]
        require(len(history)>=3 and history[0].pending_writes,'intermediate-persisted')
        await saver.finish('waiting')
        rid=saver.binding['run_id'];self.saver=saver
        async with await self.client() as client:
            path='/api/v1/runs/'+rid
            read=await client.get(path,headers=self.headers());require(read.status_code==200 and read.json()['state']=='waiting','owner-read')
            for index in (1,2,3):require((await client.get(path,headers=self.headers(index))).status_code==404,'foreign-owner')
            version=read.json()['version']
            response=await client.post(path+'/resume',headers=self.headers(0,'application/json'),json=dict(expected_version=version));require(response.status_code==202,'owner-resume')
            require((await client.post(path+'/resume',headers=self.headers(0,'application/json'),json=dict(expected_version=version))).status_code==409,'resume-replay-cas')
        self.passed('owner_read_resume_cas_and_other_owner_tenant_admin_denied')
        newlease=await self.scheduler.claim();require(newlease['id']!=saver.binding['job_id'],'new-job-id')
        fresh=BoundCheckpointSaver(saver.repository,credential_hash=self.jobs._credential_hash,tenant_id=saver.binding['tenant_id'],run_id=rid,job_id=newlease['id'],generation=newlease['generation'])
        await fresh.start(model_ref='fictional-no-inference',prompt_version='chk01-v1',config_version=1)
        require(fresh.binding['epoch']>saver.binding['epoch'],'monotonic-epoch')
        await self.rejected(saver.aget_tuple(cfg))
        print('Fresh saver resume begins',flush=True)
        resumed=await graph(fresh).ainvoke(Command(resume=True),fresh.config())
        require(resumed['value']==4 and resumed['accepted'] is True,'persistent-resume')
        self.passed('real_langgraph_interrupt_new_adapter_new_job_resume_and_pending_writes')
        page=[v async for v in fresh.alist(fresh.config(),limit=2)]
        older=[v async for v in fresh.alist(fresh.config(),before=page[-1].config,limit=20)]
        require(older and older[0].config!=page[-1].config,'history-page')
        self.passed('namespace_latest_specific_get_and_history_before_cursor')
        self.fresh=fresh

    async def beat(self,saver):
        await self.jobs.heartbeat(Lease(UUID(saver.binding['job_id']),UUID(saver.binding['tenant_id']),saver.binding['generation']))

    async def isolation(self):
        saver=self.fresh;cfg=saver.config();latest=await saver.aget_tuple(cfg)
        for change in (dict(tenant_id=str(self.people[2]['tenant'])),dict(run_id=str(uuid4())),dict(job_id=str(uuid4())),dict(generation=saver.binding['generation']+1),dict(epoch=saver.binding['epoch']-1)):
            await self.beat(saver)
            bad=BoundCheckpointSaver(saver.repository,**{k:v for k,v in {**saver.binding,**change}.items() if k!='sources'})
            await self.rejected(bad.aget_tuple(bad.config()))
            async def listing():return [v async for v in bad.alist(bad.config())]
            await self.rejected(listing())
            await self.rejected(bad.aput(latest.config,{**latest.checkpoint,'id':str(uuid4())},latest.metadata,{}))
            await self.rejected(bad.aput_writes(latest.config,[('value',7)],'fictional-task'))
        self.passed('get_put_list_intermediate_wrong_tenant_run_job_generation_epoch_denied')
        await self.rejected(saver.aget_tuple({'configurable':{}}),403)
        await self.rejected(saver.aget_tuple({'configurable':{'thread_id':str(uuid4())}}),403)
        self.passed('missing_or_foreign_thread_cannot_override_server_binding')
        await saver.aput_writes(latest.config,[('value',7)],'dedup-task')
        await saver.aput_writes(latest.config,[('value',7)],'dedup-task')
        await self.rejected(saver.aput_writes(latest.config,[('value',8)],'dedup-task'),409)
        stale=latest.parent_config
        await self.rejected(saver.aput(stale,{**latest.checkpoint,'id':str(uuid4())},latest.metadata,{}),409)
        self.passed('intermediate_retry_identical_only_and_stale_checkpoint_parent_rejected')
        for operation in ('get','put','list','writes'):
            await self.beat(saver)
            payload=saver.payload(latest.config)
            for change in (dict(epoch=None),dict(generation=None)):
                async def raw():
                    async with self.database.transaction() as connection:
                        return await saver.repository.execute(connection,operation,{**payload,**change})
                await self.rejected(raw(),400)
            async def missing():
                async with self.database.transaction() as connection:
                    return await saver.repository.execute(connection,operation,{k:v for k,v in payload.items() if k!='tenant_id'})
            await self.rejected(missing(),400)
        self.passed('direct_capability_null_epoch_generation_missing_tenant_rejected_all_storage_paths')

    async def provenance(self):
        saver=self.fresh;await self.beat(saver);cfg=saver.config();ref=SourceRef('service','claim',str(uuid4()),'1','active_reviewer','1')
        saver.binding['sources']=[dict(system=ref.system,kind=ref.kind,resource_id=ref.resource_id,version=ref.version,relation=ref.relation,observed_revision=ref.observed_revision,page=None,chunk=None)]
        await self.rejected(saver.aget_tuple(cfg),403)
        self.sources.allowed.add((saver.binding['tenant_id'],ref));authorized=await saver.aget_tuple(cfg)
        saver.binding['sources']=[]
        self.sources.allowed.clear()
        await self.rejected(saver.aget_tuple(cfg),403)
        async def listing():return [v async for v in saver.alist(cfg)]
        await self.rejected(listing(),403)
        await self.rejected(saver.aput_writes(authorized.config,[('value',2)],'source-denied'),403)
        self.passed('run_union_provenance_inherited_old_history_intermediates_deny_after_stage_revoke')
        self.sources.allowed.add((saver.binding['tenant_id'],ref))
        latest=await saver.aget_tuple(cfg);self.sources.unavailable=True
        await self.rejected(saver.aget_tuple(cfg),503);self.sources.unavailable=False
        self.passed('source_authority_outage_fails_closed_without_checkpoint_payload')
        await self.beat(saver)
        self.sources.entered=asyncio.Event();self.sources.release=asyncio.Event()
        reader=asyncio.create_task(saver.aget_tuple(cfg));revoker=None
        try:
            await asyncio.wait_for(self.sources.entered.wait(),3)
            revoker=asyncio.create_task(self.sources.revoke(saver.binding['tenant_id'],ref))
            await asyncio.sleep(.05);require(not revoker.done(),'source-revoke-waits')
            self.sources.release.set();require(await reader is not None,'pinned-read');await revoker
        finally:
            self.sources.release.set();self.sources.entered=self.sources.release=None
            for task in (reader,revoker):
                if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        await self.rejected(saver.aget_tuple(cfg),403)
        self.sources.allowed.add((saver.binding['tenant_id'],ref))
        self.passed('controlled_source_revoke_waits_for_checkpoint_commit_then_next_read_denies')
        self.sources.entered=asyncio.Event();self.sources.release=asyncio.Event()
        reader=asyncio.create_task(saver.aget_tuple(cfg))
        try:
            await asyncio.wait_for(self.sources.entered.wait(),3);reader.cancel()
            try:await reader
            except asyncio.CancelledError:pass
            else:raise CheckFailure('checkpoint-cancel-returned')
        finally:
            self.sources.release.set();self.sources.entered=self.sources.release=None
        require(self.sources.active==0 and await self.database.ping(),'cancel-cleanup')
        await saver.aget_tuple(cfg)
        self.passed('actual_checkpoint_guard_cancellation_releases_job_source_and_pool')
        await saver.finish('completed')
        require(await self.scalar('SELECT active_run_id IS NULL FROM {s}.conversations WHERE id=(SELECT conversation_id FROM {s}.runs WHERE id=%s)',(UUID(saver.binding['run_id']),)),'completed-pointer')
        await self.rejected(saver.aget_tuple(cfg))
        self.passed('terminal_completion_atomic_job_run_active_pointer_and_old_adapter_denied')

    async def takeover(self):
        saver=await self.new_run();cfg=saver.config();g=graph(saver);print('Checkpoint lifecycle start accepted',flush=True)
        await g.ainvoke({'value':0,'accepted':False},cfg)
        await self.execute("UPDATE {s}.outbox_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(UUID(saver.binding['job_id']),))
        await self.rejected(saver.aget_tuple(cfg))
        # One scheduler pass retires the expired attempt; due retry is advanced on this owned fixture only.
        await self.scheduler.claim()
        await self.execute("UPDATE {s}.outbox_jobs SET next_attempt_at=clock_timestamp() WHERE id=%s",(UUID(saver.binding['job_id']),))
        lease=await self.scheduler.claim();require(lease['generation']>saver.binding['generation'],'takeover-generation')
        fresh=BoundCheckpointSaver(saver.repository,credential_hash=self.jobs._credential_hash,tenant_id=saver.binding['tenant_id'],run_id=saver.binding['run_id'],job_id=lease['id'],generation=lease['generation'])
        await fresh.start(model_ref='fictional-no-inference',prompt_version='chk01-v1',config_version=1)
        latest=await fresh.aget_tuple(fresh.config());require(latest is not None,'takeover-state')
        await self.rejected(saver.aput_writes(latest.config,[('value',5)],'late-worker'))
        require((await graph(fresh).ainvoke(Command(resume=True),fresh.config()))['accepted'],'takeover-resume')
        await fresh.finish('completed')
        self.passed('expired_lease_scheduler_takeover_old_worker_returns_fenced_new_worker_resumes')

    async def double_interrupt(self):
        saver=await self.new_run();g=graph(saver,twice=True);cfg=saver.config()
        require((await g.ainvoke(dict(value=1,accepted=False),cfg)).get('__interrupt__'),'first-interrupt')
        require((await g.ainvoke(Command(resume=True),cfg)).get('__interrupt__'),'second-interrupt')
        require((await g.ainvoke(Command(resume=True),cfg))['accepted'],'double-resume')
        await saver.finish('completed')
        self.passed('same_node_two_interrupts_resume_control_slots_update_under_fence')

    async def concurrent_resume(self):
        saver=await self.new_run();await graph(saver).ainvoke(dict(value=0,accepted=False),saver.config());await saver.finish('waiting')
        rid=UUID(saver.binding['run_id']);version=await self.scalar('SELECT version FROM {s}.runs WHERE id=%s',(rid,))
        person=self.people[0];second_token,second_csrf=await self.session(person['tenant'],person['person']);task=None
        before=await self.scalar("SELECT jsonb_build_array(version,writer_generation,job_id,(SELECT count(*) FROM {s}.outbox_jobs WHERE target_id=%s)) FROM {s}.runs WHERE id=%s",(rid,rid))
        for value in (None,str(version),True,0,-1,1.5):
            async def malformed():
                async with self.database.transaction() as connection:
                    return await saver.repository.execute(connection,'resume',dict(token=digest('session',person['token']),id=str(rid),csrf_hash=digest('csrf',person['csrf']),expected_version=value,job_id=str(uuid4())))
            await self.rejected(malformed(),400)
        after=await self.scalar("SELECT jsonb_build_array(version,writer_generation,job_id,(SELECT count(*) FROM {s}.outbox_jobs WHERE target_id=%s)) FROM {s}.runs WHERE id=%s",(rid,rid))
        require(before==after,'invalid-resume-mutated-state')
        self.passed('direct_owner_resume_null_string_boolean_invalid_versions_leave_run_epoch_outbox_unchanged')
        headers={**self.headers(0,'application/json'),'cookie':'__Host-eai-session='+second_token,'x-csrf-token':second_csrf}
        try:
            async with self.database.transaction() as connection:
                pid=(await (await connection.execute('SELECT pg_backend_pid()')).fetchone())[0]
                first=await saver.repository.execute(connection,'resume',dict(token=digest('session',person['token']),id=str(rid),csrf_hash=digest('csrf',person['csrf']),expected_version=version,job_id=str(uuid4())))
                async def resume():
                    async with await self.client() as client:return await client.post('/api/v1/runs/'+str(rid)+'/resume',headers=headers,json=dict(expected_version=version))
                task=asyncio.create_task(resume());waiting=False
                for _ in range(40):
                    waiting=await self.scalar('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid<>%s AND %s=ANY(pg_blocking_pids(pid)))',(pid,pid))
                    if waiting:break
                    await asyncio.sleep(.025)
                require(waiting and not task.done(),'two-resume-overlap')
            response=await task;require(response.status_code==409 and first['state']=='queued','one-resume-wins')
            require(await self.scalar("SELECT count(*) FROM {s}.outbox_jobs WHERE target_id=%s AND state='queued'",(rid,))==1,'single-resume-outbox')
        finally:
            if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.passed('controlled_two_session_resume_overlap_admits_exactly_one_job')
        lease=await self.scheduler.claim()
        fresh=BoundCheckpointSaver(saver.repository,credential_hash=self.jobs._credential_hash,tenant_id=saver.binding['tenant_id'],run_id=str(rid),job_id=lease['id'],generation=lease['generation'])
        await fresh.start(model_ref='fictional-no-inference',prompt_version='chk01-v1',config_version=1);await fresh.finish('failed')

    async def private(self):
        for table in ('checkpoints','checkpoint_writes'):
            require(await self.scalar("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s",(self.schema,table)),'forced-rls')
            async with self.database.transaction() as connection:
                try:await connection.execute(sql.SQL('SELECT * FROM {}.{}').format(sql.Identifier(self.schema),sql.Identifier(table)))
                except Exception:pass
                else:raise CheckFailure('private-table-readable')
        self.passed('forced_rls_checkpoint_intermediate_tables_no_runtime_raw_grants')


async def verify(direct,base):
    schema='db03_probe_'+uuid4().hex;extension_schema='eai_vector_probe_'+uuid4().hex;owned_extension=False;owned=False;database=None;stage='extension'
    sources=source_hashes();before=await migrate(direct)
    try:
        async with direct_connection(direct) as connection:
            if not (await (await connection.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone()):
                async with connection.transaction():
                    await connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(extension_schema)))
                    await connection.execute(sql.SQL('CREATE EXTENSION vector WITH SCHEMA {}').format(sql.Identifier(extension_schema)))
                owned_extension=True
            require((await (await connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone()) is None,'namespace-unowned')
        stage='migration';await migrate(direct,schema=schema,upgrade=True);owned=True
        target=await provision_role(direct,base,schema=schema,role_name='eai_runtime_'+schema.rsplit('_',1)[1]);database=RuntimeDatabase(target,schema=schema);await database.open()
        async with direct_connection(direct,timeout=600) as connection:
            h=Harness(connection,database,schema);h.direct=direct;await h.setup_files()
            for name in ('lifecycle','isolation','provenance','takeover','double_interrupt','concurrent_resume','private'):
                stage=name;await getattr(h,name)();print('Checkpoint checks completed: '+name,flush=True)
        require(source_hashes()==sources,'source-stable');require(await migrate(direct)==before,'platform-unchanged')
        return dict(passed=True,checks=h.checks,check_count=len(h.checks),source_sha256=sources,tested_revision=12,platform_revision=before[0],platform_schema_modified=False,
          provenance_authority='controlled_pin_fixture_not_live_document_or_service_adapter',run_lifecycle='real_langgraph_fictional_interrupt_resume_no_model',actual_paid_inference_calls=0,cleanup='completed')
    except Exception as error:
        cause=error.__context__
        if cause is not None:print('Check diagnostic: '+type(cause).__name__+':'+str(getattr(cause,'sqlstate',None))+':position='+str(getattr(getattr(cause,'diag',None),'statement_position',None)),flush=True)
        raise CheckFailure(stage+':'+(str(error) if isinstance(error,CheckFailure) else type(error).__name__)) from None
    finally:
        if database is not None:await database.close()
        if owned:await drop_probe_schema(direct,schema)
        if owned_extension:
            async with direct_connection(direct) as connection:
                found=await (await connection.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'")).fetchone()
                require(found==(extension_schema,),'owned-extension-cleanup')
                await connection.execute('DROP EXTENSION vector');await connection.execute(sql.SQL('DROP SCHEMA {}').format(sql.Identifier(extension_schema)))

def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/chk01/contract-live.json'));args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required.')
    try:
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base))
        args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result);print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
    except Exception as error:print('Checkpoint verification failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None

if __name__=='__main__':main()
