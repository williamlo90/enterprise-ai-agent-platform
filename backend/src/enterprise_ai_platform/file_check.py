"""FILE-01 explicit disposable PostgreSQL/HTTP check; optional real isolated parser."""
import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import secrets
import sys
import time
from uuid import UUID,uuid4
import httpx
from psycopg import sql
from enterprise_ai_platform.authorization_check import Harness as AccessHarness,CheckFailure,require
from enterprise_ai_platform.composition import ApplicationServices,database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase,direct_connection,parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import migrate
from enterprise_ai_platform.infrastructure.database.roles import provision_role,drop_probe_schema
from enterprise_ai_platform.infrastructure.database.files import PostgresFileRepository
from enterprise_ai_platform.infrastructure.database.jobs import PostgresJobRepository
from enterprise_ai_platform.infrastructure.storage.parser import DockerFileParser
from enterprise_ai_platform.integrations.identity.cognito_probe import safe_transport_logs
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.modules.files.domain import FileFailure,Upload
from enterprise_ai_platform.modules.files.service import FilesService,FileInspection
from enterprise_ai_platform.modules.jobs.service import JobService
from enterprise_ai_platform.modules.identity.service import digest
from enterprise_ai_platform.modules.system.service import HealthService
from enterprise_ai_platform.infrastructure.runtime import RuntimeReadiness

ORIGIN='https://file-fixture.example.test'


class Storage:
    """Controlled bytes provider; not advertised as a live S3 integration."""
    def __init__(self):self.objects={};self.lost=False;self.read_hook=None;self.puts=0
    async def upload(self,key,data,digest):
        if key in self.objects:raise FileFailure(409)
        self.objects[key]=dict(data=data,digest=digest,version=secrets.token_urlsafe(16),scan='clean');self.puts+=1
        if self.lost:raise FileFailure(503)
        return self.objects[key]['version']
    async def recover(self,row):
        item=self.objects.get(row['object_key'])
        if item is None or item['scan']!='clean':return None
        require(item['digest']==row['digest'] and len(item['data'])==row['size_bytes'],'recovery-binding')
        return item['version']
    async def scan(self,row):return self.objects[row['object_key']]['scan']
    async def read(self,row):
        item=self.objects[row['object_key']];require(item['version']==row['object_version'],'version-read')
        if self.read_hook:await self.read_hook()
        return item['data']


class Parser:
    async def inspect(self,data,purpose,mime):return b'invalid-fixture' not in data


class Harness(AccessHarness):
    def __init__(self,admin,database,schema,parser):
        super().__init__(admin,database,schema)
        self.file_repo=PostgresFileRepository(database);self.store=Storage();self.files=FilesService(self.file_repo,self.store,ORIGIN)
        self.parser=parser;self.checks=[];self.people=[]

    def passed(self,name):require(name not in self.checks,'duplicate-check');self.checks.append(name)
    async def scalar(self,query,args=None):return (await (await self.execute(query,args)).fetchone())[0]

    async def caller(self,kind,tenant=None):
        secret=secrets.token_urlsafe(32);service=JobService(PostgresJobRepository(self.database),secret)
        await self.execute("INSERT INTO {s}.job_callers(id,credential_hash,capability,tenant_id,expires_at) VALUES(%s,%s,%s,%s,clock_timestamp()+interval '1 day')",(uuid4(),service._credential_hash,kind,tenant))
        return service

    async def setup_files(self):
        for n in range(4):
            tenant=self.people[0]['tenant'] if n in (1,3) else uuid4();person=uuid4()
            grants=(RoleGrant('tenant_admin','tenant',str(tenant)),) if n==3 else (RoleGrant('employee','tenant',str(tenant)),RoleGrant('knowledge_manager','tenant',str(tenant)))
            member=await self.member(tenant,person,grants=grants);token,csrf=await self.session(tenant,person)
            self.people.append(dict(tenant=tenant,person=person,member=member,token=token,csrf=csrf))
        self.jobs=await self.caller('executor',self.people[0]['tenant']);self.scheduler=await self.caller('scheduler')
        self.inspector=FileInspection(self.file_repo,self.store,self.parser,self.jobs,self.jobs._credential_hash)

    def headers(self,index=0,mime='text/plain',key=None):
        person=self.people[index]
        return {'origin':ORIGIN,'content-type':mime,'x-csrf-token':person['csrf'],'idempotency-key':key or str(uuid4()),'cookie':'__Host-eai-session='+person['token']}

    async def client(self):
        services=ApplicationServices(health=HealthService(RuntimeReadiness()),files=self.files)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=services)),base_url=ORIGIN)

    async def upload(self,data=b'Fictional policy.',index=0,purpose='knowledge',mime='text/plain',key=None):
        async with await self.client() as client:
            return await client.post('/api/v1/files?purpose='+purpose,content=data,headers=self.headers(index,mime,key))

    async def get(self,file_id,kind='status',index=0):
        async with await self.client() as client:return await client.get('/api/v1/files/'+file_id+'/'+kind,headers={'cookie':'__Host-eai-session='+self.people[index]['token']})

    async def process(self):
        claim=await self.scheduler.claim();require(claim['status']=='ok' and claim['job_type']=='file.inspect','claim-inspection')
        return await self.inspector.process(claim)

    async def basic(self):
        response=await self.upload();require(response.status_code==202,'upload-'+str(response.status_code));row=response.json();file_id=row['id']
        require(row['scan_state']=='quarantine' and set(row)=={'id','purpose','mime','size_bytes','scan_state','version'},'private-metadata')
        self.passed('upload_returns_quarantine_and_safe_metadata')
        require((await self.get(file_id,'content')).status_code==404,'unready-content')
        self.passed('unready_bytes_never_downloaded')
        require(await self.scalar('SELECT count(*) FROM {s}.outbox_jobs')==1,'atomic-inspection-job')
        self.passed('version_bind_and_inspection_outbox_atomic')
        for n in (1,2,3):
            require((await self.get(file_id,index=n)).status_code==404 and (await self.get(file_id,'content',n)).status_code==404,'scope-'+str(n))
        self.passed('colleague_foreign_tenant_and_admin_cannot_read_file')
        result=await self.process();require(result['scan_state']=='ready','inspection-ready')
        downloaded=await self.get(file_id,'content');require(downloaded.status_code==200 and downloaded.content==b'Fictional policy.','ready-download')
        require(downloaded.headers.get('cache-control')=='no-store' and downloaded.headers.get('x-content-type-options')=='nosniff' and downloaded.headers.get('content-disposition','').startswith('attachment;'),'download-headers')
        self.passed('clean_and_parsed_file_ready_proxy_matches_bytes')
        self.passed('download_private_attachment_headers')
        new,csrf=await self.session(self.people[0]['tenant'],self.people[0]['person']);old=self.people[0]['token'];self.people[0]['token']=new;self.people[0]['csrf']=csrf
        await self.execute('UPDATE {s}.sessions SET revoked_at=clock_timestamp() WHERE token_hash=%s',(digest('session',old),))
        require((await self.get(file_id,'content')).status_code==200,'new-session-access')
        self.passed('ready_file_remains_accessible_in_new_authorized_session')
        async def revoke():
            await self.execute('UPDATE {s}.sessions SET revoked_at=clock_timestamp() WHERE token_hash=%s',(digest('session',new),))
        self.store.read_hook=revoke
        require((await self.get(file_id,'content')).status_code==401,'revoked-during-read')
        self.store.read_hook=None;self.people[0]['token'],self.people[0]['csrf']=await self.session(self.people[0]['tenant'],self.people[0]['person'])
        self.passed('revocation_during_storage_await_blocks_output')

    async def recovery(self):
        key=str(uuid4());puts=self.store.puts
        a,b=await asyncio.gather(self.upload(key=key),self.upload(key=key))
        require(a.status_code==b.status_code==202 and a.json()['id']==b.json()['id'] and self.store.puts==puts+1,'concurrent-upload')
        self.passed('concurrent_same_key_creates_one_intent_and_upload')
        changed=await self.upload(b'changed',key=key);require(changed.status_code==409,'same-key-different-bytes')
        self.passed('idempotency_binds_material_bytes')
        self.store.lost=True;key=str(uuid4());before=self.store.puts
        require((await self.upload(key=key)).status_code==503,'lost-put-response')
        self.store.lost=False
        recovered=await self.upload(key=key)
        require(recovered.status_code==202 and recovered.json()['version']==2 and self.store.puts==before+1,'recover-no-new-put')
        self.passed('lost_put_response_recovered_by_exact_version_without_reupload')
        for name,put_before in (('crash_after_reserve_before_put',False),('crash_after_put_before_bind',True)):
            value=Upload('knowledge','text/plain',b'Fictional policy.',str(uuid4()));person=self.people[0]
            reserved=await self.file_repo.call(digest('session',person['token']),'reserve',dict(id=str(uuid4()),purpose=value.purpose,mime=value.mime,
                size_bytes=len(value.data),digest=value.digest,idempotency_key=value.key,collection_id=None,csrf_hash=digest('csrf',person['csrf'])))
            if put_before:await self.store.upload(reserved['object_key'],value.data,value.digest)
            before=self.store.puts
            response=await self.upload(value.data,key=value.key)
            require(response.status_code==202 and response.json()['id']==reserved['id'] and response.json()['version']==2,name)
            require(self.store.puts==before+(0 if put_before else 1),name+'-no-duplicate-put')
            self.passed(name+'_resumes_same_intent')
        value=Upload('knowledge','text/plain',b'Fictional policy.',str(uuid4()));person=self.people[0]
        reserved=await self.file_repo.call(digest('session',person['token']),'reserve',dict(id=str(uuid4()),purpose=value.purpose,mime=value.mime,
            size_bytes=len(value.data),digest=value.digest,idempotency_key=value.key,collection_id=None,csrf_hash=digest('csrf',person['csrf'])))
        for _ in range(3):await self.file_repo.call(digest('session',person['token']),'upload_attempt',dict(id=reserved['id'],version=reserved['version'],csrf_hash=digest('csrf',person['csrf'])))
        require((await self.upload(value.data,key=value.key)).status_code==429,'upload-attempt-budget')
        self.passed('durable_upload_attempt_budget_includes_pre_put_crashes')
        # Pending jobs are processed/rejected to free the per-tenant running slot.
        while (await self.scheduler.claim())['status']=='ok':
            # Expire explicitly in this disposable fixture; production never edits leases directly.
            await self.execute("UPDATE {s}.outbox_jobs SET state='held',worker_id=NULL,lease_until=NULL,attempt_deadline=NULL WHERE state='running'")

    async def http_negatives(self):
        before=self.store.puts
        async with await self.client() as client:
            for name,change,expected in (
                ('missing_csrf',{'x-csrf-token':None},403),('foreign_origin',{'origin':'https://foreign.example.test'},403),
                ('missing_session',{'cookie':None},401),('admin_has_no_upload_authority',{},403)):
                headers=self.headers(3 if name=='admin_has_no_upload_authority' else 0)
                for key,value in change.items():
                    if value is None:headers.pop(key,None)
                    else:headers[key]=value
                result=await client.post('/api/v1/files?purpose=knowledge',content=b'Fictional',headers=headers)
                require(result.status_code==expected,name+'-'+str(result.status_code));self.passed(name)
            duplicate=[*self.headers().items(),('idempotency-key',str(uuid4()))]
            require((await client.post('/api/v1/files?purpose=knowledge',content=b'x',headers=duplicate)).status_code==400,'duplicate-key')
            self.passed('duplicate_idempotency_header_rejected')
            for query in ('purpose=knowledge&purpose=receipt','purpose=knowledge&unexpected=','purpose=knowledge&purpose=','purpose=knowledge&tenant_id='+str(self.people[2]['tenant']),''):
                require((await client.post('/api/v1/files?'+query,content=b'x',headers=self.headers())).status_code==400,'invalid-query')
            self.passed('duplicate_unknown_and_missing_query_rejected')
            headers=self.headers();headers['content-length']='1'
            require((await client.post('/api/v1/files?purpose=knowledge',content=b'wrong length',headers=headers)).status_code==400,'actual-content-length')
            self.passed('content_length_mismatch_rejected')
            require((await client.post('/api/v1/files?purpose=knowledge',content=b'\xff\x00',headers=self.headers())).status_code==422,'invalid-utf8')
            self.passed('binary_as_text_rejected_before_storage')
            require((await client.post('/api/v1/files?purpose=receipt',content=b'plain text',headers=self.headers())).status_code==422,'receipt-text')
            self.passed('receipt_text_outside_allowed_formats')
            require(self.store.puts==before,'denials-no-storage')
            self.passed('rejected_http_inputs_never_upload_to_storage')
            exact=await client.post('/api/v1/files?purpose=knowledge',content=b'a'*(10*1024**2),headers=self.headers())
            require(exact.status_code==202 and exact.json()['size_bytes']==10*1024**2,'exact-http-limit')
            self.passed('actual_http_ten_mib_quarantined_without_one_mib_truncation')
            too_big=await client.post('/api/v1/files?purpose=knowledge',content=b'a'*(10*1024**2+1),headers=self.headers())
            require(too_big.status_code==413 and too_big.headers.get('cache-control')=='no-store','http-oversize')
            self.passed('actual_http_limit_plus_one_rejected_with_private_headers')
            async def chunks():
                for data in (b'Fictional ',b'streamed ',b'document.'):yield data
            streamed=await client.post('/api/v1/files?purpose=knowledge',content=chunks(),headers=self.headers())
            require(streamed.status_code==202 and streamed.json()['size_bytes']==28,'streamed-limit')
            self.passed('stream_without_content_length_counts_actual_bytes')

    async def inspection_cases(self):
        # Only this disposable fixture uses direct SQL to settle unrelated queue rows.
        await self.execute("UPDATE {s}.outbox_jobs SET state='held',worker_id=NULL,lease_until=NULL,attempt_deadline=NULL WHERE state IN ('queued','retry','running')")
        response=await self.upload(b'Threat fixture.');file_id=response.json()['id']
        key=await self.scalar('SELECT object_key FROM {s}.files WHERE id=%s',(UUID(file_id),));self.store.objects[key]['scan']='rejected'
        require((await self.process())['scan_state']=='rejected' and (await self.get(file_id,'content')).status_code==404,'threat-rejected')
        self.passed('provider_threat_never_becomes_ready_or_downloadable')
        response=await self.upload(b'%PDF-1.4\ninvalid-fixture',mime='application/pdf');file_id=response.json()['id']
        require((await self.process())['scan_state']=='rejected' and (await self.get(file_id,'content')).status_code==404,'parser-rejected')
        self.passed('provider_clean_but_corrupt_parser_result_rejected')
        response=await self.upload();file_id=response.json()['id'];key=await self.scalar('SELECT object_key FROM {s}.files WHERE id=%s',(UUID(file_id),))
        self.store.objects[key]['scan']='pending'
        require((await self.process())['state']=='retry' and (await self.get(file_id)).json()['scan_state']=='quarantine','pending-retry')
        self.passed('pending_scan_retries_without_exposing_bytes')
        await self.execute("UPDATE {s}.outbox_jobs SET state='held',worker_id=NULL,lease_until=NULL,attempt_deadline=NULL WHERE state='retry'")
        response=await self.upload();file_id=response.json()['id'];claim=await self.scheduler.claim()
        await self.execute("UPDATE {s}.outbox_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(UUID(claim['id']),))
        require((await self.scheduler.claim())['status']=='empty','reap-file-attempt')
        await self.execute("UPDATE {s}.outbox_jobs SET next_attempt_at=clock_timestamp()-interval '1 second' WHERE id=%s",(UUID(claim['id']),))
        replacement=await self.scheduler.claim();require(replacement['generation']==claim['generation']+1,'new-generation')
        try:await self.file_repo.call(None,'processed',dict(credential_hash=self.jobs._credential_hash,job_id=claim['id'],generation=claim['generation'],accepted=True))
        except FileFailure as error:require(error.status==409,'stale-inspector-status')
        else:raise CheckFailure('stale-inspector-ready')
        require((await self.get(file_id)).json()['scan_state']=='quarantine','stale-inspector-left-quarantine')
        self.passed('stale_inspector_cannot_publish_ready_after_takeover')
        require((await self.inspector.process(replacement))['scan_state']=='ready','new-inspector-ready')
        self.passed('replacement_inspector_owns_finalization')
        response=await self.upload();file_id=response.json()['id'];claim=await self.scheduler.claim()
        token=self.people[0]['token']
        await self.execute('UPDATE {s}.sessions SET revoked_at=clock_timestamp() WHERE token_hash=%s',(digest('session',token),))
        try:await self.file_repo.call(None,'processed',dict(credential_hash=self.jobs._credential_hash,job_id=claim['id'],generation=claim['generation'],accepted=True))
        except FileFailure as error:require(error.status==403,'revoked-inspector-status')
        else:raise CheckFailure('revoked-inspector-ready')
        require(await self.scalar("SELECT scan_state='quarantine' FROM {s}.files WHERE id=%s",(UUID(file_id),)),'revoked-file-state')
        self.passed('queued_actor_revocation_holds_inspection_and_preserves_quarantine')
        self.people[0]['token'],self.people[0]['csrf']=await self.session(self.people[0]['tenant'],self.people[0]['person'])

    async def private_boundaries(self):
        for name,statement in (
            ('direct_file_select_denied','SELECT * FROM {s}.files'),
            ('direct_file_update_denied',"UPDATE {s}.files SET scan_state='ready'"),
            ('private_file_policy_helper_denied','SELECT {s}._files_allowed(NULL,NULL,NULL,NULL)')):
            try:
                async with self.database.transaction() as connection:
                    await connection.execute(sql.SQL(statement).format(s=sql.Identifier(self.schema)))
            except Exception:pass
            else:raise CheckFailure(name)
            require(await self.database.ping(),'runtime-pool-after-denial');self.passed(name)

    async def atomic_bind_and_collection(self):
        value=Upload('knowledge','text/plain',b'Fictional policy.',str(uuid4()));person=self.people[0]
        reserved=await self.file_repo.call(digest('session',person['token']),'reserve',dict(id=str(uuid4()),purpose=value.purpose,mime=value.mime,
            size_bytes=len(value.data),digest=value.digest,idempotency_key=value.key,collection_id=None,csrf_hash=digest('csrf',person['csrf'])))
        version=await self.store.upload(reserved['object_key'],value.data,value.digest)
        await self.execute("CREATE FUNCTION {s}.file_failure_fixture() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Fixture failure' USING ERRCODE='23514'; END; $$")
        await self.execute('REVOKE ALL ON FUNCTION {s}.file_failure_fixture() FROM PUBLIC,{a}')
        await self.execute('CREATE TRIGGER fail_file_bind BEFORE UPDATE ON {s}.files FOR EACH ROW EXECUTE FUNCTION {s}.file_failure_fixture()')
        try:
            try:await self.file_repo.call(digest('session',person['token']),'bind',dict(id=reserved['id'],version=1,object_version=version,csrf_hash=digest('csrf',person['csrf'])))
            except FileFailure as error:require(error.status==400,'bind-rollback-status')
            else:raise CheckFailure('bind-failure-accepted')
        finally:await self.execute('DROP TRIGGER fail_file_bind ON {s}.files; DROP FUNCTION {s}.file_failure_fixture()')
        require(await self.scalar('SELECT object_version IS NULL AND inspection_job_id IS NULL AND version=1 FROM {s}.files WHERE id=%s',(UUID(reserved['id']),)),'bind-rollback-file')
        require(await self.scalar('SELECT count(*) FROM {s}.outbox_jobs WHERE target_id=%s',(UUID(reserved['id']),))==0,'bind-rollback-job')
        self.passed('failure_after_job_insert_rolls_back_bind_and_outbox_together')
        puts=self.store.puts;response=await self.upload(value.data,key=value.key)
        require(response.status_code==202 and self.store.puts==puts,'bind-rollback-recovery')
        self.passed('rolled_back_bind_retries_exact_provider_version_without_reupload')
        await self.execute("UPDATE {s}.outbox_jobs SET state='held',worker_id=NULL,lease_until=NULL,attempt_deadline=NULL WHERE state IN ('queued','retry','running')")
        collection=uuid4();tenant=self.people[0]['tenant'];actor=uuid4()
        await self.execute("INSERT INTO {s}.authorization_scopes(tenant_id,scope_kind,scope_ref) VALUES(%s,'knowledge_collection',%s)",(tenant,collection))
        member=await self.member(tenant,actor,grants=(RoleGrant('knowledge_manager','knowledge_collection',str(collection)),))
        token,csrf=await self.session(tenant,actor);self.people.append(dict(tenant=tenant,person=actor,member=member,token=token,csrf=csrf));index=len(self.people)-1
        async with await self.client() as client:
            wrong=await client.post('/api/v1/files?purpose=knowledge&collection_id='+str(uuid4()),content=b'Fictional collection.',headers=self.headers(index))
            require(wrong.status_code==403,'collection-foreign')
            self.passed('collection_manager_cannot_upload_outside_registered_scope')
            response=await client.post('/api/v1/files?purpose=knowledge&collection_id='+str(collection),content=b'Fictional collection.',headers=self.headers(index))
            require(response.status_code==202,'collection-upload')
        file_id=response.json()['id'];require((await self.process())['scan_state']=='ready','collection-inspection')
        self.passed('registered_collection_manager_upload_and_inspection')
        async def withdraw():await self.execute('UPDATE {s}.authorization_scopes SET active=false WHERE scope_ref=%s',(collection,))
        self.store.read_hook=withdraw
        try:require((await self.get(file_id,'content',index)).status_code==404,'collection-withdraw-during-read')
        finally:self.store.read_hook=None
        self.passed('collection_revocation_during_storage_await_blocks_output')

    async def collision_and_returning_worker(self):
        shared=uuid4();foreign=self.people[2]
        sid=await self.scalar('SELECT id FROM {s}.sessions WHERE token_hash=%s',(digest('session',foreign['token']),))
        revision=await self.scalar('SELECT access_revision FROM {s}.memberships WHERE tenant_id=%s AND id=%s',(foreign['tenant'],foreign['member']))
        await self.execute("INSERT INTO {s}.outbox_jobs(tenant_id,id,job_type,business_key,target_id,expected_version,actor_id,membership_id,session_id,access_revision,state) VALUES(%s,%s,'file.inspect',%s,%s,1,%s,%s,%s,%s,'completed')",
            (foreign['tenant'],shared,uuid4(),uuid4(),foreign['person'],foreign['member'],sid,revision))
        value=Upload('knowledge','text/plain',b'Fictional policy.',str(uuid4()));person=self.people[0]
        reserved=await self.file_repo.call(digest('session',person['token']),'reserve',dict(id=str(shared),purpose=value.purpose,mime=value.mime,
            size_bytes=len(value.data),digest=value.digest,idempotency_key=value.key,collection_id=None,csrf_hash=digest('csrf',person['csrf'])))
        version=await self.store.upload(reserved['object_key'],value.data,value.digest)
        await self.file_repo.call(digest('session',person['token']),'bind',dict(id=str(shared),version=1,object_version=version,csrf_hash=digest('csrf',person['csrf'])))
        require((await self.process())['id']==str(shared),'job-id-collision-owner')
        require(await self.scalar("SELECT state='completed' FROM {s}.outbox_jobs WHERE tenant_id=%s AND id=%s",(foreign['tenant'],shared)),'foreign-job-unchanged')
        self.passed('same_job_uuid_in_foreign_tenant_cannot_change_worker_binding')
        response=await self.upload();file_id=response.json()['id'];claim=await self.scheduler.claim();original=self.parser;replacement={}
        h=self
        class Delayed:
            async def inspect(self,data,purpose,mime):
                accepted=await original.inspect(data,purpose,mime)
                await h.execute("UPDATE {s}.outbox_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND id=%s",(h.people[0]['tenant'],UUID(claim['id'])))
                require((await h.scheduler.claim())['status']=='empty','late-worker-reap')
                await h.execute("UPDATE {s}.outbox_jobs SET next_attempt_at=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND id=%s",(h.people[0]['tenant'],UUID(claim['id'])))
                replacement.update(await h.scheduler.claim())
                return accepted
        worker=FileInspection(self.file_repo,self.store,Delayed(),self.jobs,self.jobs._credential_hash)
        try:await worker.process(claim)
        except Exception:pass
        else:raise CheckFailure('returning-worker-published')
        require((await self.get(file_id)).json()['scan_state']=='quarantine' and replacement['generation']==claim['generation']+1,'returning-worker-fenced')
        require((await self.inspector.process(replacement))['scan_state']=='ready','returning-worker-new-winner')
        self.passed('old_worker_returning_after_parser_and_takeover_cannot_publish')
        person=self.people[0];original_service=self.files;repo=self.file_repo
        class LateRepository:
            async def call(self,token,operation,payload):
                value=await repo.call(token,operation,payload)
                if operation=='download':await asyncio.sleep(max(0,value['authority_expires_at']-time.time())+.02)
                return value
        self.files=FilesService(LateRepository(),self.store,ORIGIN)
        await self.execute("WITH e AS(SELECT clock_timestamp()+interval '800 milliseconds' t) UPDATE {s}.sessions SET expires_at=e.t,authenticated_at=e.t-interval '8 hours',created_at=e.t-interval '8 hours',last_seen_at=clock_timestamp() FROM e WHERE token_hash=%s",(digest('session',person['token']),))
        try:require((await self.get(file_id,'content')).status_code==401,'last-db-await-expiry')
        finally:
            self.files=original_service
            await self.execute("WITH e AS(SELECT clock_timestamp() t) UPDATE {s}.sessions SET expires_at=e.t+interval '8 hours',authenticated_at=e.t,created_at=e.t,last_seen_at=e.t FROM e WHERE token_hash=%s",(digest('session',person['token']),))
        self.passed('expiry_during_last_db_await_never_releases_buffered_bytes')


async def verify(direct,base,parser):
    schema='db03_probe_'+uuid4().hex;database=None;owned=False;stage='namespace'
    try:
        before=await migrate(direct)
        async with direct_connection(direct) as connection:
            require((await (await connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone()) is None,'namespace')
        owned=True;stage='migration';await migrate(direct,schema=schema,upgrade=True)
        target=await provision_role(direct,base,schema=schema,role_name='eai_runtime_'+schema.rsplit('_',1)[1])
        database=RuntimeDatabase(target,schema=schema);await database.open()
        async with direct_connection(direct,timeout=600) as connection:
            h=Harness(connection,database,schema,parser);stage='setup';await h.setup_files()
            for name in ('basic','recovery','http_negatives','inspection_cases','private_boundaries','collision_and_returning_worker','atomic_bind_and_collection'):
                stage=name;await getattr(h,name)();print('File checks completed: '+name,flush=True)
            tested_hash=await h.scalar('SELECT checksum FROM {s}.schema_migrations WHERE version=8')
        require(await migrate(direct)==before,'platform-unchanged')
        return dict(passed=True,checks=h.checks,check_count=len(h.checks),tested_revision=8,tested_migration_sha256=tested_hash,platform_revision=before[0],platform_schema_modified=False,
                    storage_provider='controlled_memory_fixture_not_live_S3',parser='actual_isolated_Docker' if isinstance(parser,DockerFileParser) else 'controlled_fixture',disposable_cleanup='completed')
    except Exception as error:
        raise CheckFailure(stage+':'+(str(error) if isinstance(error,CheckFailure) else type(error).__name__)) from None
    finally:
        if database is not None:await database.close()
        if owned:await drop_probe_schema(direct,schema)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--parser-image')
    parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/file01/contract-live.json'));args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required.')
    safe_transport_logs()
    try:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text('{"passed":false,"status":"running"}\n',encoding='utf-8')
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        engine=DockerFileParser(args.parser_image) if args.parser_image else Parser()
        async def execute():
            try:return await verify(direct,base,engine)
            finally:
                if isinstance(engine,DockerFileParser):await engine.close()
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(execute())
        args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');print(json.dumps(result))
    except Exception as error:
        print('File verification failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_operation_failure'),file=sys.stderr);raise SystemExit(1) from None


if __name__=='__main__':main()
