"""CONV-01 disposable real DB/API checks; provenance authority is a controlled fixture."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
from pathlib import Path
import time
from uuid import UUID,uuid4
from psycopg import sql
from psycopg.types.json import Jsonb
import httpx
from enterprise_ai_platform.file_check import Harness as FileHarness,Parser,ORIGIN
from enterprise_ai_platform.knowledge_check import source_hashes,write_json
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import ApplicationServices,database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase,direct_connection,parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import migrate
from enterprise_ai_platform.infrastructure.database.roles import provision_role,drop_probe_schema
from enterprise_ai_platform.infrastructure.database.conversations import PostgresConversationRepository
from enterprise_ai_platform.modules.conversations.domain import SourceRef,ConversationFailure
from enterprise_ai_platform.modules.conversations.service import ConversationService,DenySources
from enterprise_ai_platform.modules.identity.service import digest
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.modules.system.service import HealthService
from enterprise_ai_platform.infrastructure.runtime import RuntimeReadiness
from enterprise_ai_platform.main import create_app

class ControlledSources:
    def __init__(self):self.allowed=set();self.lock=asyncio.Lock();self.entered=None;self.release=None;self.delay=0;self.unavailable=False;self.active=0
    @asynccontextmanager
    async def hold(self,subject,refs):
        async with self.lock:
            self.active+=1
            try:
                if self.unavailable:raise TimeoutError()
                if self.entered:self.entered.set()
                if self.release:await self.release.wait()
                if self.delay:await asyncio.sleep(self.delay)
                yield tuple((subject.tenant_id,ref) in self.allowed for ref in refs)
            finally:self.active-=1
    async def revoke(self,tenant,ref):
        async with self.lock:self.allowed.discard((str(tenant),ref))

class Harness(FileHarness):
    def __init__(self,*args):
        super().__init__(*args,Parser());self.conv_repo=PostgresConversationRepository(self.database);self.sources=ControlledSources();self.conversations=ConversationService(self.conv_repo,ORIGIN,sources=self.sources)
    async def client(self):
        services=ApplicationServices(health=HealthService(RuntimeReadiness()),conversations=self.conversations)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=services)),base_url=ORIGIN)
    async def create_conversation(self,index=0,language='en',key=None,headers=None):
        async with await self.client() as client:return await client.post('/api/v1/conversations',json=dict(language=language,key=key or str(uuid4())),headers=headers or self.headers(index,'application/json'))
    async def append(self,cid,text='Hello fictional assistant.',client_id=None,index=0,headers=None):
        async with await self.client() as client:return await client.post('/api/v1/conversations/'+cid+'/messages',json=dict(text=text,client_message_id=client_id or str(uuid4())),headers=headers or self.headers(index,'application/json'))
    async def history(self,cid,index=0,after=0,limit=20):
        async with await self.client() as client:return await client.get('/api/v1/conversations/'+cid+'/messages',params=dict(after=after,limit=limit),headers=self.headers(index))
    async def listing(self,index=0,after=None,limit=10):
        params=dict(limit=limit)
        if after is not None:params['after']=after
        async with await self.client() as client:return await client.get('/api/v1/conversations',params=params,headers=self.headers(index))
    async def feedback(self,mid,index=0,key=None,rating='up',reason=None):
        async with await self.client() as client:return await client.post('/api/v1/messages/'+mid+'/feedback',json=dict(key=key or str(uuid4()),rating=rating,reason=reason),headers=self.headers(index,'application/json'))
    async def model(self,append,*,body='Fictional answer.',mode='source_free',refs=(),run_refs=False):
        tenant=self.people[0]['tenant'];cid=UUID(append['conversation_id']);rid=UUID(append['run_id']);mid=uuid4()
        async with self.admin.transaction():
            sequence=await self.scalar('SELECT next_sequence FROM {s}.conversations WHERE tenant_id=%s AND id=%s FOR UPDATE',(tenant,cid))
            await self.execute("UPDATE {s}.runs SET state='completed',version=version+1 WHERE tenant_id=%s AND id=%s",(tenant,rid))
            await self.execute("UPDATE {s}.outbox_jobs SET state='completed' WHERE tenant_id=%s AND id=%s",(tenant,rid))
            await self.execute("INSERT INTO {s}.messages(tenant_id,id,conversation_id,author_kind,body,sequence,run_id,output_kind,provenance_mode) VALUES(%s,%s,%s,'model',%s,%s,%s,'answer',%s)",(tenant,mid,cid,body,sequence,rid,mode))
            await self.execute('UPDATE {s}.conversations SET active_run_id=NULL,next_sequence=next_sequence+1,version=version+1 WHERE tenant_id=%s AND id=%s',(tenant,cid))
            for ref in refs:await self.source(mid,cid,rid,ref,run_refs=run_refs)
        return str(mid)
    async def source(self,mid,cid,rid,ref,*,run_refs=False):
        await self.execute('INSERT INTO {s}.source_dependencies(tenant_id,id,conversation_id,output_message_id,run_id,source_system,resource_type,resource_id,resource_version,required_relation,observed_revision,page,chunk,platform_document_version_id,platform_file_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
          (self.people[0]['tenant'],uuid4(),cid,None if run_refs else mid,rid if run_refs else None,ref.system,ref.kind,UUID(ref.resource_id),ref.version,ref.relation,ref.observed_revision,ref.page,ref.chunk,UUID(ref.resource_id) if (ref.system,ref.kind)==('platform','document_version') else None,UUID(ref.resource_id) if (ref.system,ref.kind)==('platform','file') else None))
    async def platform_fixture(self,index=0):
        person=self.people[index];tenant=person['tenant'];fid,did,vid=uuid4(),uuid4(),uuid4();sha=hashlib.sha256(b'x').hexdigest()
        sid=await self.scalar('SELECT id FROM {s}.sessions WHERE token_hash=%s',(digest('session',person['token']),))
        async with self.admin.transaction():
            await self.execute("INSERT INTO {s}.files(tenant_id,id,owner_principal_id,membership_id,session_id,access_revision,purpose,mime,size_bytes,digest,idempotency_key,object_key) VALUES(%s,%s,%s,%s,%s,1,'knowledge','text/plain',1,%s,%s,%s)",(tenant,fid,person['person'],person['member'],sid,sha,uuid4(),'quarantine/'+str(tenant)+'/'+str(fid)))
            await self.execute("INSERT INTO {s}.documents(tenant_id,id,owner_id,title) VALUES(%s,%s,%s,'Provenance fixture')",(tenant,did,person['person']))
            await self.execute("INSERT INTO {s}.outbox_jobs(tenant_id,id,job_type,business_key,target_id,expected_version,actor_id,membership_id,session_id,access_revision) VALUES(%s,%s,'knowledge.ingest',%s,%s,1,%s,%s,%s,1)",(tenant,vid,vid,vid,person['person'],person['member'],sid))
            await self.execute("INSERT INTO {s}.document_versions(tenant_id,id,document_id,version_no,file_id,file_digest,object_version,size_bytes,mime,job_id,access_revision) VALUES(%s,%s,%s,1,%s,%s,'controlled-fixture',1,'text/plain',%s,1)",(tenant,vid,did,fid,sha,vid))
        return SourceRef('platform','document_version',str(vid),'1','read','1'),SourceRef('platform','file',str(fid),sha,'read','1')

    async def ownership_and_order(self):
        key=str(uuid4());a=await self.create_conversation(key=key);require(a.status_code==201,'create');cid=a.json()['id']
        require((await self.create_conversation(key=key)).json()==a.json(),'create-replay')
        require((await self.create_conversation(key=key,language='id')).status_code==409,'create-material-conflict')
        self.passed('server_owned_conversation_create_and_reauthorized_key_replay')
        for index in (1,2,3):
            require((await self.history(cid,index)).status_code==404 and (await self.append(cid,index=index)).status_code==404,'foreign-colleague-admin')
        require((await self.listing(1)).json()['items']==[] and (await self.listing(3)).status_code==403,'owner-list')
        self.passed('colleague_cross_tenant_admin_cannot_list_or_access_owner_history')
        client_id=str(uuid4());sent=await self.append(cid,client_id=client_id);require(sent.status_code==202,'append');row=sent.json()
        require(row['sequence']==1 and row['run_state']=='queued','queued-run')
        require((await self.append(cid,client_id=client_id)).json()==row,'append-replay')
        require((await self.append(cid,text='Changed',client_id=client_id)).status_code==409 and (await self.append(cid)).status_code==409,'dedup-and-busy')
        require(await self.scalar('SELECT count(*) FROM {s}.runs WHERE conversation_id=%s',(UUID(cid),))==1,'one-run')
        require(await self.scalar("SELECT count(*) FROM {s}.outbox_jobs WHERE id=%s AND job_type='agent.resume'",(UUID(row['run_id']),))==1,'one-job')
        self.passed('message_sequence_client_dedup_run_and_outbox_admitted_atomically')
        self.passed('changed_message_conflicts_and_pending_run_blocks_new_client_id')
        mid=await self.model(row);second=await self.append(cid,text='Second question.');require(second.status_code==202 and second.json()['sequence']==3,'next-sequence')
        page=await self.history(cid,limit=2);require([m['sequence'] for m in page.json()['messages']]==[1,2] and page.json()['next_after']==2,'message-page')
        later=await self.history(cid,after=2,limit=2);require([m['sequence'] for m in later.json()['messages']]==[3] and later.json()['next_after'] is None,'message-next-page')
        self.passed('ordered_history_keyset_cursor_preserves_human_model_message_sequence')
        own2=(await self.create_conversation()).json()['id'];listing=await self.listing(limit=1);require(len(listing.json()['items'])==1 and listing.json()['next_after'] is not None,'conversation-page')
        follow=await self.listing(after=listing.json()['next_after'],limit=20);require(cid in [c['id'] for c in follow.json()['items']],'conversation-after')
        foreign=(await self.create_conversation(index=2)).json()['id'];require((await self.listing(after=foreign)).status_code==404,'foreign-list-cursor')
        self.passed('conversation_cursor_is_owner_bound_and_created_order_stable')
        self.cid,self.input,self.model_id,self.other=cid,row,mid,own2

    async def feedback_cases(self):
        mid=self.model_id;key=str(uuid4());a=await self.feedback(mid,key=key,reason='Fictional note.');require(a.status_code==200,'feedback')
        require((await self.feedback(mid,key=key,reason='Fictional note.')).json()==a.json(),'feedback-replay')
        require((await self.feedback(mid,key=key,rating='down')).status_code==409,'feedback-key-conflict')
        b=await self.feedback(mid,rating='down');require(b.status_code==200 and b.json()['feedback_version']==2,'feedback-change')
        old=await self.feedback(mid,key=key,reason='Fictional note.');require(old.json()['current_rating']=='down' and old.json()['result_version']==1,'old-feedback-no-reapply')
        require((await self.feedback(self.input['message_id'])).status_code==404,'no-human-feedback')
        for index in (1,2,3):require((await self.feedback(mid,index=index)).status_code==404,'foreign-feedback')
        self.passed('feedback_owner_model_only_keyed_idempotency_and_old_receipt_no_reapply')

    async def admission_rollback_and_races(self):
        cid=(await self.create_conversation()).json()['id'];client_id=str(uuid4())
        await self.execute("CREATE FUNCTION {s}.conv_failure_fixture() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Fixture failure' USING ERRCODE='23514'; END; $$")
        await self.execute('REVOKE ALL ON FUNCTION {s}.conv_failure_fixture() FROM PUBLIC,{a}')
        await self.execute('CREATE TRIGGER conv_failure_fixture BEFORE INSERT ON {s}.messages FOR EACH ROW EXECUTE FUNCTION {s}.conv_failure_fixture()')
        try:require((await self.append(cid,client_id=client_id)).status_code==400,'append-failure')
        finally:await self.execute('DROP TRIGGER conv_failure_fixture ON {s}.messages;DROP FUNCTION {s}.conv_failure_fixture()')
        require(await self.scalar('SELECT next_sequence=1 AND active_run_id IS NULL FROM {s}.conversations WHERE id=%s',(UUID(cid),)),'rollback-conversation')
        require(await self.scalar('SELECT count(*) FROM {s}.runs WHERE conversation_id=%s',(UUID(cid),))==0,'rollback-run')
        require(await self.scalar("SELECT count(*) FROM {s}.outbox_jobs WHERE actor_id=%s AND job_type='agent.resume' AND target_id NOT IN (SELECT id FROM {s}.runs)",(self.people[0]['person'],))==0,'rollback-outbox')
        self.passed('failure_after_run_outbox_insert_rolls_back_message_sequence_pointer_and_jobs')
        token,csrf=await self.session(self.people[0]['tenant'],self.people[0]['person']);headers={**self.headers(0,'application/json'),'cookie':'__Host-eai-session='+token,'x-csrf-token':csrf}
        a,b=await asyncio.gather(self.append(cid,client_id=client_id),self.append(cid,client_id=client_id,headers=headers));require(a.status_code==b.status_code==202 and a.json()['run_id']==b.json()['run_id'],'two-session-dedup')
        self.passed('two_sessions_same_client_message_choose_one_sequence_run_and_job')

    async def provenance_cases(self):
        doc,file=await self.platform_fixture();foreign,_=await self.platform_fixture(2)
        service=SourceRef('service','claim',str(uuid4()),'1','active_reviewer','stage1')
        cid=(await self.create_conversation()).json()['id'];sent=(await self.append(cid)).json()
        mid=await self.model(sent,body='PRIVATE-DOC-AND-CLAIM',mode='dependent',refs=(doc,service));tenant=str(self.people[0]['tenant'])
        self.sources.allowed.update((tenant,ref) for ref in (doc,service))
        require((await self.history(cid)).json()['messages'][1]['text']=='PRIVATE-DOC-AND-CLAIM','allowed-provenance')
        await self.feedback(mid,reason='PRIVATE-FEEDBACK')
        await self.sources.revoke(tenant,service)
        value=(await self.history(cid)).json()['messages'][1];require(value['redacted'] and value['text'] is None and value['sources']==[] and value['feedback'] is None,'stage-prune')
        require('PRIVATE' not in (await self.history(cid)).text,'no-stale-derived-body')
        self.passed('typed_document_and_service_provenance_stage_loss_redacts_body_refs_and_feedback')
        self.sources.allowed.add((tenant,service));self.conversations.sources=DenySources()
        require((await self.history(cid)).json()['messages'][1]['redacted'],'default-deny')
        self.conversations.sources=self.sources;self.sources.unavailable=True
        try:require((await self.history(cid)).status_code==503,'source-unavailable')
        finally:self.sources.unavailable=False
        self.passed('default_unknown_sources_redact_and_dependency_outage_fails_closed')
        second=(await self.append(cid,text='Follow-up')).json();unknown=await self.model(second,body='UNVERIFIED-SECRET',mode='unverified')
        require((await self.history(cid)).json()['messages'][-1]['redacted'],'unverified-output')
        third=(await self.append(cid,text='Run dependent')).json();run_mid=await self.model(third,body='RUN-SOURCE-SECRET',mode='source_free',refs=(service,),run_refs=True)
        await self.sources.revoke(tenant,service)
        require((await self.history(cid)).json()['messages'][-1]['redacted'],'run-inheritance')
        self.passed('unverified_output_and_inherited_run_dependency_cannot_bypass_redaction')
        for bad in (foreign,SourceRef('platform','document_version',doc.resource_id,'99','read','1')):
            try:
                async with self.admin.transaction():await self.source(UUID(mid),UUID(cid),UUID(sent['run_id']),bad)
            except Exception:pass
            else:raise CheckFailure('bad-platform-source-accepted')
        self.passed('platform_source_fk_rejects_foreign_tenant_and_wrong_document_version')
        self.prov_cid,self.prov_ref=cid,doc

    async def exact_bindings(self):
        a=(await self.create_conversation()).json()['id'];b=(await self.create_conversation()).json()['id'];left=(await self.append(a)).json();right=(await self.append(b)).json()
        for query,args in [
          ("UPDATE {s}.messages SET run_id=%s WHERE id=%s",(UUID(right['run_id']),UUID(left['message_id']))),
          ("UPDATE {s}.runs SET input_message_id=%s WHERE id=%s",(UUID(right['message_id']),UUID(left['run_id']))),
          ("UPDATE {s}.conversations SET active_run_id=%s WHERE id=%s",(UUID(right['run_id']),UUID(a)))]:
            try:
                async with self.admin.transaction():await self.execute(query,args)
            except Exception:pass
            else:raise CheckFailure('cross-conversation-binding')
        self.passed('message_run_input_and_active_pointer_require_exact_same_conversation')
        ref=SourceRef('service','trip',str(uuid4()),'1','owner','1')
        try:
            async with self.admin.transaction():await self.source(UUID(left['message_id']),UUID(a),UUID(left['run_id']),ref)
        except Exception:pass
        else:raise CheckFailure('human-source-injected')
        self.passed('provenance_cannot_be_attached_as_hidden_instruction_to_human_input')

    async def controlled_dedup(self):
        cid=(await self.create_conversation()).json()['id'];person=self.people[0];client_id=str(uuid4());second_token,second_csrf=await self.session(person['tenant'],person['person'])
        headers={**self.headers(0,'application/json'),'cookie':'__Host-eai-session='+second_token,'x-csrf-token':second_csrf};task=None
        try:
            async with self.database.transaction() as connection:
                pid=(await (await connection.execute('SELECT pg_backend_pid()')).fetchone())[0]
                first=await self.conv_repo.execute(connection,digest('session',person['token']),'append',dict(id=cid,message_id=str(uuid4()),run_id=str(uuid4()),client_message_id=client_id,text='Barrier message.',csrf_hash=digest('csrf',person['csrf'])))
                task=asyncio.create_task(self.append(cid,text='Barrier message.',client_id=client_id,headers=headers))
                deadline=time.monotonic()+3;waiting=False
                while time.monotonic()<deadline:
                    waiting=await self.scalar('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid<>%s AND %s=ANY(pg_blocking_pids(pid)))',(pid,pid))
                    if waiting:break
                    await asyncio.sleep(.05)
                require(waiting and not task.done(),'second-session-never-overlapped')
            replay=await task;require(replay.status_code==202 and replay.json()['run_id']==first['run_id'] and replay.json()['message_id']==first['message_id'],'barrier-dedup-outcome')
        finally:
            if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        require(await self.scalar('SELECT count(*) FROM {s}.messages WHERE conversation_id=%s',(UUID(cid),))==1,'barrier-one-message')
        self.passed('controlled_two_session_message_overlap_waits_then_replays_one_admission')

    async def guard_lifetime(self):
        tenant=str(self.people[0]['tenant']);ref=SourceRef('service','trip',str(uuid4()),'1','owner','1');cid=(await self.create_conversation()).json()['id'];sent=(await self.append(cid)).json()
        await self.model(sent,body='PINNED-PRIVATE-SOURCE',mode='dependent',refs=(ref,));self.sources.allowed.add((tenant,ref))
        self.sources.entered=asyncio.Event();self.sources.release=asyncio.Event();read=None;revoke=None;started=asyncio.Event()
        async def revoke_source():started.set();await self.sources.revoke(tenant,ref)
        try:
            read=asyncio.create_task(self.history(cid));await asyncio.wait_for(self.sources.entered.wait(),3)
            revoke=asyncio.create_task(revoke_source());await asyncio.wait_for(started.wait(),3)
            require(not revoke.done() and self.sources.active==1,'source-authority-not-pinned')
            self.sources.release.set();response=await read;require(response.status_code==200 and response.json()['messages'][1]['text']=='PINNED-PRIVATE-SOURCE','pinned-read-consistent');await revoke
        finally:
            for task in (read,revoke):
                if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
            self.sources.entered=self.sources.release=None
        require((await self.history(cid)).json()['messages'][1]['redacted'],'next-read-after-revoke')
        self.passed('source_guard_pins_fixture_authority_through_owner_commit_and_next_read_redacts')
        self.sources.entered=asyncio.Event();self.sources.release=asyncio.Event();task=None
        try:
            task=asyncio.create_task(self.conversations.history(self.people[0]['token'],cid));await asyncio.wait_for(self.sources.entered.wait(),3);task.cancel()
            try:await task
            except asyncio.CancelledError:pass
            else:raise CheckFailure('cancelled-history-returned-content')
        finally:
            self.sources.entered=self.sources.release=None
            if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        require(self.sources.active==0 and await self.database.ping(),'cancelled-guard-pool-cleanup')
        self.passed('history_cancellation_releases_guard_and_owner_transaction_without_output')
        # Session/membership revocation cannot commit while history holds its owner UoW.
        self.sources.entered=asyncio.Event();self.sources.release=asyncio.Event();read=None;writer=None;admin_pid=await self.scalar('SELECT pg_backend_pid()')
        try:
            read=asyncio.create_task(self.history(cid));await asyncio.wait_for(self.sources.entered.wait(),3)
            writer=asyncio.create_task(self.execute('UPDATE {s}.memberships SET active=false WHERE id=%s',(self.people[0]['member'],)))
            async with direct_connection(self.direct) as observer:
                deadline=time.monotonic()+3;waiting=False
                while time.monotonic()<deadline:
                    waiting=(await (await observer.execute('SELECT cardinality(pg_blocking_pids(%s))>0',(admin_pid,))).fetchone())[0]
                    if waiting:break
                    await asyncio.sleep(.05)
                require(waiting and not writer.done(),'owner-revocation-not-pinned')
            self.sources.release.set();require((await read).status_code==200,'owner-prerevoke-consistent');await writer
        finally:
            for task in (read,writer):
                if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
            self.sources.entered=self.sources.release=None
        require((await self.history(cid)).status_code==403,'next-read-owner-revoked')
        await self.execute('UPDATE {s}.memberships SET active=true WHERE id=%s',(self.people[0]['member'],))
        self.passed('owner_membership_revoke_waits_for_read_commit_and_blocks_next_history')

    async def commit_failure(self):
        cid=self.prov_cid
        await self.execute("CREATE FUNCTION {s}.history_commit_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Fixture failure' USING ERRCODE='23514'; END; $$")
        await self.execute('REVOKE ALL ON FUNCTION {s}.history_commit_failure() FROM PUBLIC,{a}')
        await self.execute('CREATE CONSTRAINT TRIGGER history_commit_failure AFTER UPDATE ON {s}.sessions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {s}.history_commit_failure()')
        try:
            response=await self.history(cid);require(response.status_code==503 and 'PRIVATE' not in response.text,'commit-failure-buffer-leak')
        finally:await self.execute('DROP TRIGGER history_commit_failure ON {s}.sessions;DROP FUNCTION {s}.history_commit_failure()')
        require(self.sources.active==0 and await self.database.ping(),'commit-failure-resource-release')
        self.passed('history_commit_failure_releases_source_guard_without_returning_buffer')

    async def same_conversation_input_guard(self):
        original=UUID(self.input['run_id']);mid=UUID(self.input['message_id']);other=await self.scalar('SELECT id FROM {s}.runs WHERE conversation_id=%s AND id<>%s LIMIT 1',(UUID(self.cid),original))
        for query,args in [('UPDATE {s}.messages SET run_id=%s WHERE id=%s',(other,mid)),
          ("UPDATE {s}.messages SET author_kind='model',author_principal_id=NULL,client_message_id=NULL,output_kind='answer' WHERE id=%s",(mid,))]:
            try:
                async with self.admin.transaction():await self.execute(query,args)
            except Exception:pass
            else:raise CheckFailure('same-conversation-input-invariant')
        require(await self.scalar("SELECT run_id=%s AND author_kind='human' FROM {s}.messages WHERE id=%s",(original,mid)),'input-rollback')
        self.passed('message_mutation_cannot_rebind_input_to_same_conversation_other_run_or_change_author')

    async def cancellation_after_yield(self):
        # Inject a controlled SQL barrier in the final recheck phase, after source
        # guard yield, using the same owner connection that the adapter will finish.
        key=int.from_bytes(hashlib.sha256(('conv-recheck-'+str(uuid4())).encode()).digest()[:8],'big',signed=True)
        await self.execute('SELECT pg_advisory_lock(%s)',(key,));base_execute=self.conv_repo.execute;pid=[];entered=asyncio.Event();task=None
        async def blocked(connection,token,operation,payload):
            if operation=='history_recheck':
                pid.append((await (await connection.execute('SELECT pg_backend_pid()')).fetchone())[0]);entered.set()
                await connection.execute('SELECT pg_advisory_xact_lock(%s)',(key,))
            return await base_execute(connection,token,operation,payload)
        self.conv_repo.execute=blocked
        try:
            task=asyncio.create_task(self.conversations.history(self.people[0]['token'],self.prov_cid));await asyncio.wait_for(entered.wait(),3)
            deadline=time.monotonic()+3;waiting=False
            while time.monotonic()<deadline:
                waiting=await self.scalar("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND locktype='advisory' AND NOT granted)",(pid[0],))
                if waiting:break
                await asyncio.sleep(.05)
            require(waiting and self.sources.active==1,'recheck-cancel-phase')
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
            else:raise CheckFailure('recheck-cancellation-returned')
        finally:
            self.conv_repo.execute=base_execute
            if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
            await self.execute('SELECT pg_advisory_unlock(%s)',(key,))
        require(self.sources.active==0 and await self.database.ping(),'recheck-cancel-cleanup')
        self.passed('cancellation_after_guard_yield_during_final_recheck_releases_source_and_owner_uow')

    async def cancellation_during_commit(self):
        key=int.from_bytes(hashlib.sha256(('conv-commit-'+str(uuid4())).encode()).digest()[:8],'big',signed=True)
        await self.execute('SELECT pg_advisory_lock(%s)',(key,))
        await self.execute("CREATE FUNCTION {s}.history_commit_pause() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_advisory_xact_lock("+str(key)+");RETURN NEW; END; $$")
        await self.execute('REVOKE ALL ON FUNCTION {s}.history_commit_pause() FROM PUBLIC,{a}')
        await self.execute('CREATE CONSTRAINT TRIGGER history_commit_pause AFTER UPDATE ON {s}.sessions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {s}.history_commit_pause()')
        task=None
        try:
            task=asyncio.create_task(self.conversations.history(self.people[0]['token'],self.prov_cid));deadline=time.monotonic()+3;waiting=False
            while time.monotonic()<deadline:
                waiting=await self.scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=%s AND query='COMMIT' AND wait_event_type='Lock')",('eai_runtime_'+self.schema.rsplit('_',1)[1],))
                if waiting:break
                await asyncio.sleep(.05)
            require(waiting and self.sources.active==1,'commit-cancel-phase')
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
            else:raise CheckFailure('commit-cancellation-returned')
        finally:
            if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
            await self.execute('SELECT pg_advisory_unlock(%s)',(key,))
            await self.execute('DROP TRIGGER history_commit_pause ON {s}.sessions;DROP FUNCTION {s}.history_commit_pause()')
        require(self.sources.active==0 and await self.database.ping(),'commit-cancel-cleanup')
        self.passed('cancellation_while_commit_waits_keeps_source_guard_until_abort_and_reuses_pool')

    async def expiry_and_private(self):
        person=self.people[0];cid=self.prov_cid;self.sources.delay=.5
        await self.execute("WITH e AS(SELECT clock_timestamp()+interval '300 milliseconds' t) UPDATE {s}.sessions SET expires_at=e.t,authenticated_at=e.t-interval '8 hours',created_at=e.t-interval '8 hours',last_seen_at=clock_timestamp() FROM e WHERE token_hash=%s",(digest('session',person['token']),))
        try:require((await self.history(cid)).status_code==401,'expiry-after-source-await')
        finally:
            self.sources.delay=0
            await self.execute("WITH e AS(SELECT clock_timestamp() t) UPDATE {s}.sessions SET expires_at=e.t+interval '8 hours',authenticated_at=e.t,created_at=e.t,last_seen_at=e.t,revoked_at=NULL FROM e WHERE token_hash=%s",(digest('session',person['token']),))
        self.passed('session_expiry_during_provenance_guard_releases_no_history')
        for table in ('conversations','messages','runs','source_dependencies','feedback','conversation_requests'):
            async with self.database.transaction() as connection:
                try:await connection.execute(sql.SQL('SELECT * FROM {}.{}').format(sql.Identifier(self.schema),sql.Identifier(table)))
                except Exception:pass
                else:raise CheckFailure('private-table-readable')
        await self.execute('GRANT SELECT ON {s}.messages TO {a}')
        try:
            try:await self.conv_repo.call(digest('session',person['token']),'list',dict(after=None,limit=1))
            except ConversationFailure:pass
            else:raise CheckFailure('runtime-drift')
        finally:await self.execute('REVOKE SELECT ON {s}.messages FROM {a}')
        self.passed('conversation_tables_private_and_runtime_privilege_drift_rejected')

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
            for name in ('ownership_and_order','feedback_cases','admission_rollback_and_races','provenance_cases','exact_bindings','controlled_dedup','guard_lifetime','commit_failure','same_conversation_input_guard','cancellation_after_yield','cancellation_during_commit','expiry_and_private'):
                stage=name;await getattr(h,name)();print('Conversation checks completed: '+name,flush=True)
        require(source_hashes()==sources,'source-stable');require(await migrate(direct)==before,'platform-unchanged')
        return dict(passed=True,checks=h.checks,check_count=len(h.checks),source_sha256=sources,tested_revision=11,platform_revision=before[0],platform_schema_modified=False,
          provenance_authority='controlled_pin_fixture_not_live_document_or_service_adapter',run_lifecycle='queued_foundation_plus_admin_output_fixtures_not_CHK01',actual_paid_inference_calls=0,cleanup='completed')
    except Exception as error:raise CheckFailure(stage+':'+(str(error) if isinstance(error,CheckFailure) else type(error).__name__)) from None
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
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/conv01/contract-live.json'));args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required.')
    try:
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base))
        args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result);print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
    except Exception as error:print('Conversation verification failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None

if __name__=='__main__':main()
