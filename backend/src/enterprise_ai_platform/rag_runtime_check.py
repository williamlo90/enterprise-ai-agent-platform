"""RAG-02 disposable DB/HTTP/graph checks; no cloud or paid model calls."""
import argparse
import asyncio
from contextlib import contextmanager
import json
import logging
from pathlib import Path
import re
from threading import Event
from types import SimpleNamespace
from uuid import UUID,uuid4
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.rag_check import Harness as RagHarness,Models,EN
from enterprise_ai_platform.knowledge_check import Extractor,source_hashes,write_json
from enterprise_ai_platform.authorization_check import CheckFailure,require
from enterprise_ai_platform.composition import database_loop_factory,build_rag_turn
from enterprise_ai_platform.config import DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase,direct_connection,parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.conversations import PostgresConversationRepository
from enterprise_ai_platform.infrastructure.database.migration import migrate
from enterprise_ai_platform.infrastructure.database.roles import provision_role,drop_probe_schema
from enterprise_ai_platform.modules.conversations.service import ConversationService
from enterprise_ai_platform.modules.identity.service import digest
from enterprise_ai_platform.modules.rag.domain import RagFailure
from enterprise_ai_platform.modules.models.ports import Usage
from enterprise_ai_platform.integrations.checkpoints.rag_graph import build_rag_graph
from enterprise_ai_platform.file_check import ORIGIN

class Harness(RagHarness):
    async def setup_runtime(self):
        await self.setup_rag()
        self.conv_repo=PostgresConversationRepository(self.database,live_sources=True)
        self.conversations=ConversationService(self.conv_repo,ORIGIN)
    async def diagnostics(self,rid,index=4):
        async with await self.client() as client:return await client.get('/api/v1/runs/'+rid+'/diagnostics',headers=self.headers(index))
    async def recover(self,rid,version,headers=None):
        async with await self.client() as client:return await client.post('/api/v1/runs/'+rid+'/recover',json=dict(expected_version=version),headers=headers or self.headers(4,'application/json'))
    async def happy(self):
        worker,cid,models=await self.turn();result=await worker.run(build_rag_graph);rid=worker.repository.binding['run_id']
        first=await self.history(cid,index=4);last=first.json()['messages'][-1]
        require(last['text'] and last['citations'] and not last['redacted'],'live-source-no-fixture')
        for _ in range(3):
            require((await self.history(cid,index=4)).json()==first.json(),'reconnect-stable')
            require((await self.diagnostics(rid)).status_code==200,'diagnostic-reconnect')
        require(models[0].calls==['embedding','chat'],'no-reconnect-inference')
        row=(await self.diagnostics(rid)).json();require(row['state']=='completed' and row['usage']['unknown_attempts']==0 and row['usage']['attempts']==2,'known-diagnostics')
        require(row['usage']['reserved_input_tokens']==4608 and row['usage']['reserved_output_tokens']==256 and row['steps']==2,'budget-diagnostics')
        require(any(v['phase']=='publish' and v['outcome']=='completed' for v in row['events']),'durable-completion-trace')
        raw=json.dumps(row)
        for value in (worker.question,EN,worker.ctx.prompt,*worker.receipts.values(),self.people[4]['token'],self.people[4]['csrf']):require(value not in raw,'trace-redaction')
        for index in (0,1,2,3):require((await self.diagnostics(rid,index)).status_code==404,'nonowner-diagnostics')
        self.completed_worker,self.completed_cid=worker,cid
        self.passed('live_history_citations_feedback_guard_and_reconnect_without_model_or_job_replay')
        self.passed('owner_diagnostics_known_reserved_tokens_duration_allowlist_no_prompt_quote_secret_receipt')
    async def history_changes(self):
        worker=self.completed_worker;cid=self.completed_cid
        selected={v['version_id'] for v in worker.ctx.hits}
        target=next(v for v in self.docs if v['version_id'] in selected)
        require((await self.mutate(target,'acl',readers=[])).status_code==200,'revoke-history-acl')
        row=(await self.history(cid,index=4)).json()['messages'][-1];require(row['redacted'] and row['text'] is None and row['sources']==row['citations']==[] and row['feedback'] is None,'whole-history-redaction')
        require((await self.diagnostics(worker.repository.binding['run_id'])).status_code==200,'metadata-after-revoke')
        require((await self.mutate(target,'acl',readers=[str(self.people[4]['person'])])).status_code==200,'restore-current-reader')
        require(not (await self.history(cid,index=4)).json()['messages'][-1]['redacted'],'current-grant-not-stale-revision')
        replacement=await self.new_version(target,'Replacement policy: claims must be submitted within 9 days.')
        require((await self.mutate(replacement,'publish',expected=(await self.current(replacement))['document_version'])).status_code==200,'replace-history-source')
        require(not (await self.history(cid,index=4)).json()['messages'][-1]['redacted'],'superseded-history-allowed')
        require((await self.mutate(target,'withdraw')).status_code==200,'withdraw-old-source')
        require((await self.history(cid,index=4)).json()['messages'][-1]['redacted'],'withdraw-history-denied')
        self.passed('acl_revoke_restore_superseded_history_and_withdraw_rechecked_without_cached_grant')
    async def budgets(self):
        worker,_,_=await self.turn();rid=UUID(worker.repository.binding['run_id'])
        start=await worker.repository.runtime('begin',{});deadline=start['deadline']
        for _ in range(8):await worker.repository.runtime('step',dict(phase='retrieve'))
        await self.rejected(worker.repository.runtime('step',dict(phase='generate')),429)
        require(await self.scalar('SELECT steps FROM {s}.rag_runtime WHERE run_id=%s',(rid,))==8,'durable-step-cap')
        require(abs(float(await self.scalar('SELECT extract(epoch FROM deadline) FROM {s}.rag_runtime WHERE run_id=%s',(rid,)))-deadline)<.0001,'no-budget-reset')
        await worker.repository.call('hold',{})
        current=(await self.diagnostics(str(rid))).json();require(current['recovery_available'],'held-recovery')
        require((await self.recover(str(rid),current['version'])).status_code==200,'close-step-budget')
        self.passed('durable_step_limit_and_deadline_do_not_reset_on_repeat_begin')
        worker,_,_=await self.turn();rid=UUID(worker.repository.binding['run_id'])
        await self.execute("UPDATE {s}.rag_runtime SET deadline=clock_timestamp()-interval '1 second' WHERE run_id=%s",(rid,))
        await self.rejected(worker.repository.call('reserve',dict(kind='embedding',items=[],request_digest='a'*64)),403)
        await worker.repository.call('hold',{})
        self.passed('expired_durable_run_budget_prevents_provider_reservation')
    async def reservation_race(self):
        worker,_,models=await self.turn();rid=UUID(worker.repository.binding['run_id'])
        values=dict(kind='embedding',items=[],request_digest='a'*64)
        async with self.database.transaction() as connection:
            first=(await (await connection.execute(sql.SQL('SELECT {}.rag_call(%s,%s)').format(sql.Identifier(self.schema)),('reserve',Jsonb({**worker.repository.binding,**values})))).fetchone())[0]
            require(first['status']=='ok','first-budget-reservation')
            await self.rejected(worker.repository.call('reserve',values),503)
        require(await self.scalar('SELECT count(*) FROM {s}.rag_attempts WHERE run_id=%s',(rid,))==1,'one-concurrent-reservation')
        await worker.repository.settle('embedding',first['settlement_receipt'],None)
        row=(await self.diagnostics(str(rid))).json()['usage']
        require(row['attempts']==row['unknown_attempts']==1 and row['reserved_input_tokens']==512 and row['effective_input_tokens']==512 and models==[],'unknown-budget-not-reclaimed')
        await worker.repository.call('hold',{})
        self.passed('controlled_overlapping_provider_reservations_admit_once_unknown_keeps_full_budget')
    async def settlements(self):
        worker,_,_=await self.turn();rid=UUID(worker.repository.binding['run_id'])
        row=await worker.repository.call('reserve',dict(kind='embedding',items=[],request_digest='a'*64));receipt=row['settlement_receipt']
        await self.execute("UPDATE {s}.outbox_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(UUID(worker.lease.id.hex),))
        await worker.repository.settle('embedding',receipt,Usage(12,0));await worker.repository.settle('embedding',receipt,Usage(12,0))
        await self.rejected(worker.repository.settle('embedding',receipt,Usage(13,0)),503)
        await self.rejected(worker.repository.settle('embedding','b'*64,Usage(12,0)),503)
        await self.rejected(worker.repository.call('search',dict(query='claims',vector=[1.0]+[0.0]*1023,mode='vector')))
        state=(await self.diagnostics(str(rid))).json();require(state['usage']['known_input_tokens']==12,'late-usage-fact')
        require((await self.recover(str(rid),state['version'])).status_code==200,'recover-expired-writer')
        self.passed('late_receipt_usage_settlement_idempotent_after_expired_lease_without_reviving_writer')
        worker,_,_=await self.turn();rid=UUID(worker.repository.binding['run_id'])
        row=await worker.repository.call('reserve',dict(kind='embedding',items=[],request_digest='c'*64));person=self.people[4]
        await self.execute('UPDATE {s}.sessions SET revoked_at=clock_timestamp() WHERE token_hash=%s',(digest('session',person['token']),))
        try:
            await worker.repository.settle('embedding',row['settlement_receipt'],Usage(17,0))
            await self.rejected(worker.repository.call('recheck',dict(items=[])))
            require(await self.scalar("SELECT input_tokens FROM {s}.rag_attempts WHERE run_id=%s AND kind='embedding'",(rid,))==17,'revoked-session-late-usage')
        finally:await self.execute('UPDATE {s}.sessions SET revoked_at=NULL WHERE token_hash=%s',(digest('session',person['token']),))
        await worker.repository.call('hold',{})
        self.passed('narrow_settlement_receipt_records_usage_after_session_revoke_without_content_authority')
    async def cancellation(self):
        worker,cid,_=await self.turn();entered=Event();release=Event();models=[]
        @contextmanager
        def factory(observer):
            def hook():entered.set();require(release.wait(5),'provider-release')
            value=Models(observer,hook=hook);models.append(value);yield SimpleNamespace(chat=value,embedding=value)
        worker.model_factory=factory;task=asyncio.create_task(worker.run(build_rag_graph))
        try:
            for _ in range(400):
                if entered.is_set():break
                if task.done():await task;raise CheckFailure('provider-not-entered')
                await asyncio.sleep(.025)
            require(entered.is_set(),'sdk-inflight');task.cancel()
            for _ in range(80):
                row=(await self.diagnostics(worker.repository.binding['run_id'])).json()
                if row.get('state')=='held':break
                await asyncio.sleep(.01)
            require(row.get('state')=='held' and not task.done(),'held-before-second-cancel');task.cancel();await asyncio.sleep(.02);release.set()
            try:await task
            except asyncio.CancelledError:pass
            else:raise CheckFailure('cancel-returned-answer')
        finally:release.set();await asyncio.gather(task,return_exceptions=True)
        rid=worker.repository.binding['run_id'];row=(await self.diagnostics(rid)).json()
        require(row['state']=='held' and row['usage']['unknown_attempts']==0,'cancel-settlement')
        require(await self.scalar("SELECT count(*) FROM {s}.messages WHERE run_id=%s AND author_kind='model'",(UUID(rid),))==0,'no-cancelled-output')
        require(await self.database.ping(),'cancel-pool-reusable')
        recovered=await self.recover(rid,row['version']);require(recovered.status_code==200 and recovered.json()['state']=='failed','explicit-safe-close')
        admitted=await self.append(cid,text='A new explicit question.',index=4);require(admitted.status_code==202,'conversation-unlocked')
        claim=await self.scheduler.claim();fresh=await build_rag_turn(self.database,self.jobs,claim,model_factory=factory);await fresh.saver.finish('failed')
        require(models[0].calls==['embedding','chat'],'no-hidden-paid-retry')
        self.passed('cancel_inflight_sdk_drains_records_usage_holds_no_output_and_owner_closes_without_replay')

    async def overrun(self):
        worker,_,_=await self.turn();rid=worker.repository.binding['run_id']
        row=await worker.repository.call('reserve',dict(kind='embedding',items=[],request_digest='a'*64))
        await worker.repository.settle('embedding',row['settlement_receipt'],Usage(5000,0))
        await self.rejected(worker.repository.call('reserve',dict(kind='chat',items=[],request_digest='b'*64)),429)
        await self.rejected(worker.repository.runtime('step',dict(phase='generate')),429)
        usage=(await self.diagnostics(rid)).json()['usage']
        require(usage['known_input_tokens']==usage['effective_input_tokens']==5000 and usage['overrun'] and usage['attempts']==1,'actual-overrun-preserved')
        await worker.repository.call('hold',{})
        self.passed('known_actual_token_overrun_retained_vetoes_next_step_and_provider_reservation')

    async def trace_allowlist(self):
        worker,_,_=await self.turn();rid=UUID(worker.repository.binding['run_id'])
        for values in (dict(phase='provider',outcome='started',duration_ms=1,prompt='RAW-SECRET-SENTINEL'),dict(phase='RAW-SECRET-SENTINEL',outcome='started',duration_ms=1),dict(phase='provider',outcome='RAW-SECRET-SENTINEL',duration_ms=1)):
            await self.rejected(worker.repository.runtime('event',values),400)
        raw=await self.scalar('SELECT coalesce(jsonb_agg(to_jsonb(e))::text,\'[]\') FROM {s}.rag_events e WHERE run_id=%s',(rid,))
        require('RAW-SECRET-SENTINEL' not in raw,'raw-trace-inserted')
        for _ in range(22):await worker.repository.runtime('event',dict(phase='provider',outcome='started',duration_ms=1))
        require(await self.scalar('SELECT count(*) FROM {s}.rag_events WHERE run_id=%s',(rid,))==20,'trace-bound')
        await self.execute("UPDATE {s}.rag_events SET created_at=clock_timestamp()-interval '8 days' WHERE run_id=%s",(rid,))
        require((await self.diagnostics(str(rid))).json()['events']==[],'trace-ttl-view')
        await worker.repository.call('hold',{})
        self.passed('trace_fields_enums_count_and_visibility_retention_reject_raw_content')

    async def deadline_publish(self):
        worker,_,_=await self.turn();original=worker.repository.call;waited=False
        async def call(operation,payload):
            nonlocal waited
            if operation!='publish':return await original(operation,payload)
            rid=UUID(worker.repository.binding['run_id'])
            await self.execute("UPDATE {s}.rag_runtime SET deadline=clock_timestamp()+interval '450 milliseconds' WHERE run_id=%s",(rid,))
            task=None
            try:
                async with self.admin.transaction():
                    pid=await self.scalar('SELECT pg_backend_pid()')
                    await self.execute('SELECT id FROM {s}.documents WHERE id=%s FOR UPDATE',(UUID(worker.ctx.hits[0]['document_id']),))
                    task=asyncio.create_task(original(operation,payload))
                    for _ in range(30):
                        waited=await self.scalar('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid<>%s AND %s=ANY(pg_blocking_pids(pid)))',(pid,pid))
                        if waited:break
                        await asyncio.sleep(.01)
                    require(waited,'publication-wait-barrier');await asyncio.sleep(.5)
                return await task
            finally:
                if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        worker.repository.call=call
        try:await worker.run(build_rag_graph)
        except Exception:pass
        else:raise CheckFailure('expired-publication-committed')
        require(waited,'publish-reached-core-lock')
        require(await self.scalar("SELECT count(*) FROM {s}.messages WHERE run_id=%s AND author_kind='model'",(UUID(worker.repository.binding['run_id']),))==0,'deadline-output')
        self.passed('publication_source_lock_wait_crossing_deadline_rolls_back_message_and_completion')

    async def timeout_inflight(self):
        worker,_,_=await self.turn();entered=Event();release=Event();models=[];rid=UUID(worker.repository.binding['run_id']);loop=asyncio.get_running_loop()
        @contextmanager
        def factory(observer):
            def hook():
                entered.set()
                async def expire():await self.execute("UPDATE {s}.rag_runtime SET deadline=clock_timestamp()-interval '1 second' WHERE run_id=%s",(rid,))
                asyncio.run_coroutine_threadsafe(expire(),loop).result(timeout=3)
                require(release.wait(3),'late-provider-release')
            value=Models(observer,hook=hook);models.append(value);yield SimpleNamespace(chat=value,embedding=value)
        worker.model_factory=factory;task=asyncio.create_task(worker.run(build_rag_graph))
        try:
            for _ in range(400):
                if entered.is_set():break
                await asyncio.sleep(.02)
            require(entered.is_set(),'timeout-inflight')
            release.set()
            try:await task
            except Exception:pass
            else:raise CheckFailure('late-timeout-answer')
        finally:release.set();await asyncio.gather(task,return_exceptions=True)
        row=(await self.diagnostics(str(rid))).json()
        require(row['usage']['attempts']==2 and row['usage']['unknown_attempts']==0 and row['state']=='held','late-known-settlement')
        require(await self.scalar("SELECT count(*) FROM {s}.messages WHERE run_id=%s AND author_kind='model'",(rid,))==0,'late-model-output')
        self.passed('sdk_result_after_durable_deadline_records_cost_but_never_publishes_or_retries')

    async def local_timeout(self):
        worker,_,_=await self.turn();entered=Event();release=Event();models=[];rid=UUID(worker.repository.binding['run_id'])
        await self.execute("UPDATE {s}.rag_runtime SET deadline=clock_timestamp()+interval '8 seconds' WHERE run_id=%s",(rid,))
        @contextmanager
        def factory(observer):
            def hook():entered.set();require(release.wait(12),'timeout-cleanup-release')
            value=Models(observer,hook=hook);models.append(value);yield SimpleNamespace(chat=value,embedding=value)
        worker.model_factory=factory;task=asyncio.create_task(worker.run(build_rag_graph))
        try:
            for _ in range(600):
                if worker.stopping.is_set():break
                await asyncio.sleep(.02)
            require(entered.is_set() and worker.stopping.is_set() and not task.done(),'deadline-while-sdk-running')
            release.set()
            try:await task
            except TimeoutError:pass
            else:raise CheckFailure('timeout-not-raised')
        finally:release.set();await asyncio.gather(task,return_exceptions=True)
        row=(await self.diagnostics(str(rid))).json()
        require(row['state']=='held' and row['usage']['unknown_attempts']==0 and any(v['outcome']=='deadline' for v in row['events']),'timeout-trace-and-late-settlement')
        require(await self.scalar("SELECT count(*) FROM {s}.messages WHERE run_id=%s AND author_kind='model'",(rid,))==0,'no-timeout-publish')
        self.passed('wall_timeout_stops_new_steps_while_sdk_still_running_then_drains_and_records_late_usage')

    async def lost_response(self):
        worker,cid,models=await self.turn();original=worker.repository.call;committed=False
        async def call(operation,payload):
            nonlocal committed
            result=await original(operation,payload)
            if operation=='publish':committed=True;raise RagFailure(503)
            return result
        worker.repository.call=call
        try:await worker.run(build_rag_graph)
        except RagFailure:pass
        else:raise CheckFailure('lost-response-not-injected')
        rid=worker.repository.binding['run_id'];require(committed,'actual-commit-before-loss')
        row=(await self.diagnostics(rid)).json();require(row['state']=='completed','committed-state-preserved')
        client_id=str(await self.scalar('SELECT client_message_id FROM {s}.messages WHERE run_id=%s AND author_kind=\'human\'',(UUID(rid),)))
        for _ in range(2):
            replay=await self.append(cid,text=worker.question,client_id=client_id,index=4)
            require(replay.status_code==202 and replay.json()['run_id']==rid and replay.json()['run_state']=='completed','idempotent-admission-reconnect')
            require(not (await self.history(cid,index=4)).json()['messages'][-1]['redacted'],'read-durable-output')
        require(models[0].calls==['embedding','chat'],'lost-response-no-inference-replay')
        require(await self.scalar('SELECT count(*) FROM {s}.outbox_jobs WHERE target_id=%s',(UUID(rid),))==1,'one-original-job')
        self.passed('lost_response_after_publication_commit_reconnects_to_one_durable_answer_without_new_job_or_inference')

    async def recovery_race(self):
        worker,_,_=await self.turn();await worker.repository.call('hold',{});rid=worker.repository.binding['run_id'];person=self.people[4]
        row=(await self.diagnostics(rid)).json();version=row['version'];token,csrf=await self.session(person['tenant'],person['person'])
        headers={**self.headers(4,'application/json'),'cookie':'__Host-eai-session='+token,'x-csrf-token':csrf}
        async with self.database.transaction() as connection:
            payload=dict(id=rid,expected_version=version,csrf_hash=digest('csrf',person['csrf']))
            first=(await (await connection.execute(sql.SQL('SELECT {}.rag_owner_call(%s,%s,%s)').format(sql.Identifier(self.schema)),(digest('session',person['token']),'recover',Jsonb(payload)))).fetchone())[0]
            require(first['status']=='ok','first-recovery')
            # First transaction is deliberately still open while independent
            # session two reaches NOWAIT/CAS; one recovery wins.
            second=await self.recover(rid,version,headers=headers);require(second.status_code==409,'concurrent-recovery-conflict')
        final=(await self.diagnostics(rid)).json();require(final['state']=='failed' and final['recovered_at'] is not None and final['version']==version+1,'recovery-atomic-audit')
        require((await self.recover(rid,version)).status_code==409,'stale-recovery-cas')
        await self.rejected(worker.repository.call('recheck',dict(items=[])))
        require(await self.scalar('SELECT count(*) FROM {s}.outbox_jobs WHERE target_id=%s',(UUID(rid),))==1,'recover-no-new-job')
        for index in (0,1,2,3):require((await self.diagnostics(rid,index)).status_code==404,'recover-other-owner')
        self.passed('two_independent_session_recovery_overlap_one_cas_audit_no_requeue_and_old_writer_fenced')

    async def history_barriers(self):
        cid=self.completed_cid;worker=self.completed_worker;target=next(v for v in self.docs if v['version_id'] in {x['version_id'] for x in worker.ctx.hits})
        original=self.conv_repo.execute;entered=asyncio.Event();release=asyncio.Event();reader_pid=None
        async def execute(connection,token,operation,payload):
            nonlocal reader_pid
            if operation=='history_recheck':
                reader_pid=(await (await connection.execute('SELECT pg_backend_pid()')).fetchone())[0]
                entered.set();await release.wait()
            return await original(connection,token,operation,payload)
        self.conv_repo.execute=execute;reader=asyncio.create_task(self.history(cid,index=4));writer=None
        try:
            await asyncio.wait_for(entered.wait(),5)
            writer=asyncio.create_task(self.mutate(target,'acl',readers=[]));waiting=False
            for _ in range(50):
                waiting=await self.scalar('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid<>%s AND %s=ANY(pg_blocking_pids(pid)))',(reader_pid,reader_pid))
                if waiting:break
                await asyncio.sleep(.01)
            require(waiting and not writer.done(),'real-acl-writer-waits-history-source-locks')
            release.set();require(not (await reader).json()['messages'][-1]['redacted'],'read-before-revoke-commit')
            require((await writer).status_code==200,'acl-commit-after-reader')
        finally:
            release.set();self.conv_repo.execute=original
            for task in (reader,writer):
                if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        require((await self.history(cid,index=4)).json()['messages'][-1]['redacted'],'next-read-live-revoke')
        require((await self.mutate(target,'acl',readers=[str(person['person']) for person in (self.people[4],)])).status_code==200,'restore-history-fixture')
        self.passed('live_history_guard_pins_source_until_commit_acl_writer_waits_next_read_redacts')
        entered.clear();release.clear();self.conv_repo.execute=execute;reader=asyncio.create_task(self.history(cid,index=4))
        try:
            await asyncio.wait_for(entered.wait(),5);reader.cancel()
            try:await reader
            except asyncio.CancelledError:pass
            else:raise CheckFailure('history-cancel-returned')
        finally:release.set();self.conv_repo.execute=original;await asyncio.gather(reader,return_exceptions=True)
        require(await self.database.ping() and (await self.history(cid,index=4)).status_code==200,'history-cancel-cleanup')
        self.passed('live_source_history_cancel_at_final_recheck_releases_guard_and_pool_without_buffer')


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
            h=Harness(connection,database,schema,Extractor());h.direct=direct;await h.setup_runtime()
            for name in ('happy','reservation_race','budgets','settlements','cancellation','overrun','trace_allowlist','deadline_publish','timeout_inflight','local_timeout','lost_response','recovery_race','history_barriers','history_changes'):
                stage=name;await getattr(h,name)();print('RAG runtime checks completed: '+name,flush=True)
        require(source_hashes()==sources,'source-stable');require(await migrate(direct)==before,'platform-unchanged')
        return dict(passed=True,checks=h.checks,check_count=len(h.checks),source_sha256=sources,tested_revision=14,platform_revision=before[0],platform_schema_modified=False,
          parser_embedding_storage='controlled_fixtures_no_paid_or_cloud_calls',cleanup='completed')
    except Exception as error:
        cause=error;seen=set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause));print('Sanitized diagnostic: '+type(cause).__name__+':status='+str(getattr(cause,'status',None))+':'+str(getattr(cause,'sqlstate',None))+':'+str(re.findall(r'line [0-9]+',str(getattr(getattr(cause,'diag',None),'context','')))),flush=True);cause=cause.__context__
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
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/rag02/contract-live.json'));args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required.')
    try:
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base))
        args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result);print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
    except Exception as error:print('RAG verification failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None

if __name__=='__main__':main()
