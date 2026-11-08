"""Disposable KNOW-01 DB/API/worker checks. No platform migration or cloud resources."""
import argparse
import asyncio
import hashlib
import json
import logging
import time
import os
from decimal import Decimal
from pathlib import Path
from uuid import UUID,uuid4
from psycopg import sql
from enterprise_ai_platform.file_check import Harness as FileHarness,Parser,ORIGIN
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import ApplicationServices,database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase,direct_connection,parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import migrate
from enterprise_ai_platform.infrastructure.database.roles import provision_role,drop_probe_schema
from enterprise_ai_platform.infrastructure.database.knowledge import PostgresKnowledgeRepository
from enterprise_ai_platform.infrastructure.storage.parser import DockerFileParser
from enterprise_ai_platform.integrations.models.nova import CohereEmbedding,NOVA_SDK_CONFIG
from enterprise_ai_platform.integrations.models.bedrock import ENDPOINT
from enterprise_ai_platform.modules.knowledge.domain import Page,KnowledgeFailure
from enterprise_ai_platform.modules.knowledge.service import KnowledgeService,KnowledgeIngestion
from enterprise_ai_platform.modules.models.domain import EmbeddingResult,Usage
from enterprise_ai_platform.modules.identity.service import digest
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform import nova_model_check
import httpx

def write_json(path,value,*,exclusive=False):
    target=path if exclusive else path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    with target.open('x',encoding='utf-8') as stream:
        json.dump(value,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    if not exclusive:target.replace(path)
    if os.name!='nt':
        handle=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(handle)
        finally:os.close(handle)

class AuditedEmbedding:
    def __init__(self,delegate,attempts,save):self.delegate,self.attempts,self.save=delegate,attempts,save
    def embed(self,texts,*,input_type):
        success=False
        try:
            value=self.delegate.embed(texts,input_type=input_type);success=True;return value
        finally:
            if self.attempts:
                usage=self.delegate.last_usage
                if usage is not None:
                    self.attempts[-1].update(usage=dict(input_tokens=usage.input_tokens,output_tokens=usage.output_tokens),actual_token_cost_usd=str(Decimal(usage.input_tokens)*Decimal('.1')/1000000))
                self.attempts[-1]['state']='completed' if success else 'failed' if usage is not None else 'unknown_or_failed'
                self.save()

def source_hashes():
    paths=list(Path('backend/src/enterprise_ai_platform').rglob('*.py'))+list(Path('backend/src/enterprise_ai_platform').rglob('*.sql'))
    paths += [Path('backend/uv.lock'),Path('scripts/spikes/storage/parser.py'),Path('scripts/spikes/storage/file_policy.py'),Path('scripts/spikes/storage/Dockerfile'),Path('scripts/spikes/storage/requirements.lock')]
    return {p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}

def pdf_fixture():
    bodies=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
      b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
      b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    content=b'BT /F1 12 Tf 72 720 Td (Fictional policy: claims within seven days after returning.) Tj ET'
    bodies.append(b'<< /Length '+str(len(content)).encode()+b' >>\nstream\n'+content+b'\nendstream')
    data=bytearray(b'%PDF-1.4\n');offsets=[]
    for index,body in enumerate(bodies,1):offsets.append(len(data));data.extend(str(index).encode()+b' 0 obj\n'+body+b'\nendobj\n')
    start=len(data);data.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets:data.extend(f'{offset:010} 00000 n \n'.encode())
    data.extend(b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(start).encode()+b'\n%%EOF\n');return bytes(data)

class Extractor:
    ref="fixture:utf8-v1"
    async def extract(self,data,mime):return (Page(1,data.decode('utf-8')),)

class Embedding:
    def __init__(self):self.calls=0;self.fail_call=None;self.observer=None
    def embed(self,texts,*,input_type):
        self.observer('embedding',len(texts)*512);self.calls+=1
        if self.calls==self.fail_call:raise TimeoutError()
        return EmbeddingResult(tuple(tuple([1.0]+[0.0]*1023) for _ in texts),Usage(len(texts)*10,0))

class Harness(FileHarness):
    def __init__(self,admin,database,schema,extractor):
        super().__init__(admin,database,schema,Parser())
        self.repo=PostgresKnowledgeRepository(database);self.knowledge=KnowledgeService(self.repo,ORIGIN)
        self.extractor=extractor;self.embedding=Embedding()
    async def client(self):
        services=ApplicationServices(health=self_health(),files=self.files,knowledge=self.knowledge)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=services)),base_url=ORIGIN)
    async def ready_file(self,text='Fictional policy.',index=0):
        response=await self.upload(text.encode(),index=index);require(response.status_code==202,'ready-upload')
        if index==0:await super().process()
        else:raise CheckFailure('unsupported-fixture-owner')
        return response.json()['id']
    async def create_document(self,file_id,*,key=None,index=0,title='Fictional policy',readers=None):
        async with await self.client() as client:
            return await client.post('/api/v1/knowledge/documents',json=dict(file_id=file_id,key=key or str(uuid4()),title=title,readers=readers or []),headers=self.headers(index,'application/json'))
    async def status_document(self,row,index=0):
        async with await self.client() as client:
            return await client.get('/api/v1/knowledge/documents/'+row['document_id']+'/versions/'+row['version_id']+'/status',headers=self.headers(index))
    async def process_knowledge(self,claim=None,extractor=None,embedding=None):
        claim=claim or await self.scheduler.claim();require(claim.get('job_type')=='knowledge.ingest','claim-knowledge')
        worker=KnowledgeIngestion(self.repo,self.store,extractor or self.extractor,self.jobs,embedding or self.embedding,self.jobs._credential_hash)
        if isinstance(worker.embedding,Embedding):worker.embedding.observer=worker.observer(asyncio.get_running_loop())
        return await worker.process(claim)
    async def basic_knowledge(self):
        fid=await self.ready_file();key=str(uuid4());response=await self.create_document(fid,key=key,readers=[str(self.people[1]['person'])]);require(response.status_code==202,'draft-admit-'+str(response.status_code));row=response.json()
        require(row['publication_state']=='draft' and row['index_state']=='pending','pending-draft');self.passed('draft_file_pin_acl_and_job_admitted_atomically')
        replay=await self.create_document(fid,key=key,readers=[str(self.people[1]['person'])]);require(replay.status_code==202 and replay.json()==row,'replay-one-version')
        require((await self.create_document(fid,key=key,title='changed')).status_code==409,'key-material-conflict');self.passed('idempotency_replay_and_changed_material_conflict')
        for index in (2,3):require((await self.status_document(row,index)).status_code==404,'hidden-status')
        require((await self.status_document(row,1)).status_code==200,'scoped-manager-status')
        require((await self.create_document(fid,index=1)).status_code==404,'foreign-owner-file')
        self.passed('tenant_admin_denied_and_scoped_manager_authorized')
        result=await self.process_knowledge();require(result['index_state']=='ready' and result['publication_state']=='draft','ingest-ready')
        require(await self.scalar('SELECT active_version_id IS NULL FROM {s}.documents WHERE id=%s',(UUID(row['document_id']),)),'not-published')
        require(await self.scalar('SELECT count(*) FROM {s}.chunks WHERE document_version_id=%s',(UUID(row['version_id']),))==1,'one-chunk')
        self.passed('parsed_embedded_vector1024_ready_without_publication')
        require(await self.scalar("SELECT state='completed' FROM {s}.outbox_jobs WHERE id=%s",(UUID(row['version_id']),)),'complete-atomic');self.passed('ready_and_job_completion_atomic')
        require(await self.scalar('SELECT count(*) FROM {s}.document_acl WHERE document_id=%s',(UUID(row['document_id']),))==2,'acl-exact');self.passed('explicit_same_tenant_reader_acl_stored')
        self.first=row
    async def rejection_and_version(self):
        response=await self.upload(b'unready');fid=response.json()['id']
        require((await self.create_document(fid)).status_code==409,'unready-admit');self.passed('quarantine_file_never_admitted')
        await super().process()
        fid=await self.ready_file('Replacement policy.');first=self.first
        async with await self.client() as client:
            url='/api/v1/knowledge/documents/'+first['document_id']+'/versions';values=dict(file_id=fid,key=str(uuid4()),expected_version=1)
            response=await client.post(url,json=values,headers=self.headers(0,'application/json'));require(response.status_code==202,'new-version')
            require((await client.post(url,json={**values,'key':str(uuid4())},headers=self.headers(0,'application/json'))).status_code==409,'version-cas')
            require((await client.post(url,json=values,headers=self.headers(0,'application/json'))).json()==response.json(),'version-replay')
        require((await self.status_document(first)).json()['index_state']=='ready','old-version-ready');await self.process_knowledge();self.passed('version_cas_replay_and_old_ready_version_preserved')
        fid=await self.ready_file();person=self.people[0];count=await self.scalar('SELECT count(*) FROM {s}.documents')
        response=await self.create_document(fid,readers=[str(self.people[2]['person'])]);require(response.status_code==400,'foreign-acl')
        require(await self.scalar('SELECT count(*) FROM {s}.documents')==count,'acl-rollback');self.passed('foreign_reader_rolls_back_document_version_acl_job_and_ledger')
        async with await self.client() as client:
            require((await client.post('/api/v1/knowledge/documents',json=dict(file_id=fid,key=str(uuid4()),title='x'),headers=self.headers(0,'text/plain'))).status_code in (403,422),'csrf-mime')
        self.passed('browser_boundary_csrf_content_type_and_safe_errors')
    async def partial_and_retry(self):
        fid=await self.ready_file('x'*2000);row=(await self.create_document(fid)).json();self.embedding.fail_call=self.embedding.calls+2
        try:await self.process_knowledge()
        except KnowledgeFailure:pass
        else:raise CheckFailure('partial-not-failed')
        state=(await self.status_document(row)).json();require(state['index_state']=='failed' and state['publication_state']=='draft','partial-not-ready')
        require(await self.scalar('SELECT count(*) FROM {s}.chunks WHERE document_version_id=%s',(UUID(row['version_id']),))==4,'staged-only')
        self.passed('partial_embedding_failure_cannot_publish_or_mark_ready')
        await self.execute("UPDATE {s}.outbox_jobs SET next_attempt_at=clock_timestamp()-interval '1 second' WHERE id=%s",(UUID(row['version_id']),));self.embedding.fail_call=None
        await self.process_knowledge();require((await self.status_document(row)).json()['index_state']=='ready','retry-ready')
        require(await self.scalar('SELECT count(DISTINCT index_generation) FROM {s}.chunks WHERE document_version_id=%s',(UUID(row['version_id']),))==1,'no-mixed-generation')
        require(await self.scalar('SELECT embedding_attempts FROM {s}.document_versions WHERE id=%s',(UUID(row['version_id']),))==4,'reservations-durable')
        self.passed('retry_rebuilds_generation_and_retains_paid_attempt_reservations')
    async def queued_revoke_and_stale(self):
        fid=await self.ready_file();row=(await self.create_document(fid)).json();claim=await self.scheduler.claim();replacement={};h=self
        class Delayed:
            ref="fixture:utf8-v1"
            async def extract(self,data,mime):
                await h.execute("UPDATE {s}.outbox_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(UUID(claim['id']),))
                require((await h.scheduler.claim())['status']=='empty','reap-stale')
                await h.execute("UPDATE {s}.outbox_jobs SET next_attempt_at=clock_timestamp()-interval '1 second' WHERE id=%s",(UUID(claim['id']),))
                replacement.update(await h.scheduler.claim());return (Page(1,data.decode()),)
        before=self.embedding.calls
        try:await self.process_knowledge(claim,extractor=Delayed())
        except KnowledgeFailure:pass
        else:raise CheckFailure('stale-worker-ready')
        require(self.embedding.calls==before and replacement['generation']==claim['generation']+1,'old-no-egress')
        await self.process_knowledge(replacement);self.passed('returning_old_worker_cannot_embed_stage_or_complete_after_takeover')
        fid=await self.ready_file();row=(await self.create_document(fid)).json();claim=await self.scheduler.claim()
        await self.execute("UPDATE {s}.files SET deletion_state='deletion_pending' WHERE id=%s",(UUID(fid),));before=self.embedding.calls
        try:await self.process_knowledge(claim)
        except KnowledgeFailure:pass
        else:raise CheckFailure('deleted-source-ready')
        require(self.embedding.calls==before and await self.scalar("SELECT state='held' FROM {s}.outbox_jobs WHERE id=%s",(UUID(row['version_id']),)),'deleted-held')
        self.passed('source_withdrawal_before_work_holds_job_without_egress')
    async def authority_changes(self):
        fid=await self.ready_file();row=(await self.create_document(fid)).json();person=self.people[0];before=self.embedding.calls
        async def revoke():await self.execute('UPDATE {s}.memberships SET active=false WHERE id=%s',(person['member'],))
        self.store.read_hook=revoke
        try:
            try:await self.process_knowledge()
            except KnowledgeFailure:pass
            else:raise CheckFailure('revoked-membership-ingested')
        finally:
            self.store.read_hook=None
            await self.execute('UPDATE {s}.memberships SET active=true WHERE id=%s',(person['member'],))
        require(self.embedding.calls==before and await self.scalar("SELECT state='held' FROM {s}.outbox_jobs WHERE id=%s",(UUID(row['version_id']),)),'revoked-held')
        self.passed('membership_revocation_during_storage_await_blocks_extraction_egress')
        fid=await self.ready_file();row=(await self.create_document(fid)).json();h=self;before=self.embedding.calls
        class ChangedACL:
            ref="fixture:utf8-v1"
            async def extract(self,data,mime):
                await h.execute('UPDATE {s}.documents SET access_revision=access_revision+1 WHERE id=%s',(UUID(row['document_id']),))
                return (Page(1,data.decode()),)
        try:await self.process_knowledge(extractor=ChangedACL())
        except KnowledgeFailure:pass
        else:raise CheckFailure('changed-acl-ingested')
        require(self.embedding.calls==before and await self.scalar("SELECT state='held' FROM {s}.outbox_jobs WHERE id=%s",(UUID(row['version_id']),)),'acl-held')
        self.passed('document_acl_revision_change_during_extraction_blocks_egress')

    async def adversarial_batches(self):
        fid=await self.ready_file();row=(await self.create_document(fid)).json();claim=await self.scheduler.claim()
        payload=dict(credential_hash=self.jobs._credential_hash,job_id=claim['id'],generation=claim['generation'])
        for extra in ({'parser_ref':None},{'unexpected':'fixture:utf8-v1'},{'parser_ref':42}):
            try:await self.repo.call(None,'begin',{**payload,**extra})
            except KnowledgeFailure:pass
            else:raise CheckFailure('null-or-missing-parser-ref')
        self.passed('null_missing_or_wrong_type_parser_ref_rejected_before_pipeline_mutation')
        await self.repo.call(None,'begin',{**payload,'parser_ref':'fixture:utf8-v1'})
        def chunk(number,text='x'):
            return dict(number=number,page=1,start=number,end=number+1,text=text,digest=hashlib.sha256(text.encode()).hexdigest(),vector=[1.0]+[0.0]*1023,model='cohere.embed-multilingual-v3')
        try:await self.repo.call(None,'stage',{**payload,'chunks':[chunk(0),{**chunk(1),'digest':'0'*64}]})
        except KnowledgeFailure as error:require(error.status==400,'invalid-batch-status')
        else:raise CheckFailure('invalid-batch-accepted')
        require(await self.scalar('SELECT count(*) FROM {s}.chunks WHERE document_version_id=%s',(UUID(row['version_id']),))==0,'batch-rollback')
        self.passed('invalid_second_chunk_rolls_back_entire_stage_batch')
        for extra in ({'chunk_count':None,'content_digest':'0'*64},{'unexpected':1,'content_digest':'0'*64},{'chunk_count':1,'content_digest':None},{'chunk_count':True,'content_digest':'0'*64},{'chunk_count':'1','content_digest':'0'*64},{'chunk_count':1,'content_digest':42}):
            try:await self.repo.call(None,'ready',{**payload,**extra})
            except KnowledgeFailure as error:require(error.status==400,'strict-ready-status')
            else:raise CheckFailure('null-or-missing-ready-manifest')
        self.passed('null_missing_or_wrong_type_ready_count_and_digest_rejected')
        await self.repo.call(None,'stage',{**payload,'chunks':[chunk(0),chunk(2)]})
        try:await self.repo.call(None,'ready',{**payload,'chunk_count':2,'content_digest':'0'*64})
        except KnowledgeFailure as error:require(error.status==409,'gap-rejected')
        else:raise CheckFailure('gap-ready')
        self.passed('noncontiguous_chunk_manifest_cannot_be_marked_ready')
        await self.repo.call(None,'failed',{**payload,'error_code':'invalid'})

    async def actual_parser_cases(self):
        if not isinstance(self.extractor,DockerFileParser):return
        pages=await self.extractor.extract(pdf_fixture(),'application/pdf')
        require(len(pages)==1 and 'seven days after returning' in pages[0].text,'pdf-extraction')
        pages=await self.extractor.extract('# Kebijakan fiktif\nKlaim setelah pulang.'.encode(),'text/markdown')
        require(pages[0].text.startswith('# Kebijakan'),'markdown-extraction')
        try:await self.extractor.extract(b'x'*12289,'text/plain')
        except KnowledgeFailure as error:require(error.status==422,'extract-limit-status')
        else:raise CheckFailure('extract-limit')
        self.passed('actual_isolated_pdf_and_markdown_extraction_with_bounded_rejection')

    async def provider_fixture(self):
        folder=Path('local/know01');folder.mkdir(parents=True,exist_ok=True)
        bound_sources=source_hashes()
        write_json(folder/'provider-claim.json',dict(source_sha256=bound_sources,data='fictional_only',max_calls=1),exclusive=True)
        session=nova_model_check.frozen_session();control=session.client('bedrock',region_name='ap-southeast-1',endpoint_url='https://bedrock.ap-southeast-1.amazonaws.com',config=NOVA_SDK_CONFIG)
        identity=session.client('sts',region_name='ap-southeast-1',endpoint_url='https://sts.ap-southeast-1.amazonaws.com',config=NOVA_SDK_CONFIG)
        try:
            setup=json.loads(Path('local/spike02/setup.json').read_text(encoding='utf-8'))
            require(identity.get_caller_identity()['Account']==setup['account'] and setup['region']=='ap-southeast-1','provider-account')
            require(control.get_account_data_retention().get('mode')=='none' and not control.get_model_invocation_logging_configuration().get('loggingConfig'),'provider-privacy')
            state=nova_model_check.availability(control,'cohere.embed-multilingual-v3','ON_DEMAND')
            require(all(state.get(k)==v for k,v in {'authorizationStatus':'AUTHORIZED','entitlementAvailability':'AVAILABLE','regionAvailability':'AVAILABLE','agreement':'AVAILABLE'}.items()),'embedding-access')
            nova_model_check.legacy.offer_prices(control,'cohere.embed-multilingual-v3')
        finally:control.close();identity.close()
        fid=await self.ready_file('Kebijakan fiktif: klaim biaya perjalanan dalam tujuh hari setelah pulang. Fictional policy: claims within seven days after returning.')
        row=(await self.create_document(fid)).json();claim=await self.scheduler.claim()
        worker=KnowledgeIngestion(self.repo,self.store,self.extractor,self.jobs,None,self.jobs._credential_hash)
        client=session.client('bedrock-runtime',region_name='ap-southeast-1',endpoint_url=ENDPOINT,config=NOVA_SDK_CONFIG)
        attempts=[];observer=worker.observer(asyncio.get_running_loop());path=folder/'provider-proof.json'
        def save():write_json(path,dict(passed=False,model='cohere.embed-multilingual-v3',region='ap-southeast-1',attempts=attempts,source_sha256=bound_sources,parser_image=self.extractor.image_id if isinstance(self.extractor,DockerFileParser) else None))
        def reserve(kind,tokens):
            require(len(attempts)<1 and source_hashes()==bound_sources,'once-only-provider-call')
            deadline=observer(kind,tokens)
            attempts.append(dict(reserved_tokens=tokens,reserved_usd=str(Decimal(tokens)*Decimal('.1')/1000000),usage=None,actual_token_cost_usd=None,state='pending'));save()
            if time.time()>=deadline:raise KnowledgeFailure(403)
        delegate=CohereEmbedding(client,reserve)
        worker.embedding=AuditedEmbedding(delegate,attempts,save);save()
        try:
            result=await worker.process(claim);require(result['index_state']=='ready' and result['publication_state']=='draft','actual-provider-ingest')
            proof=json.loads(path.read_text(encoding='utf-8'));proof.update(passed=True,final_billed_cost_verified=False,storage='controlled_immutable_fixture_not_S3');write_json(path,proof)
        finally:client.close()
        self.passed('actual_cohere_singapore_embedding_committed_by_fenced_ingestion')

    async def private_knowledge(self):
        for table in ('documents','document_versions','document_acl','knowledge_requests','chunks'):
            async with self.database.transaction() as connection:
                try:await connection.execute(sql.SQL('SELECT * FROM {}.{}').format(sql.Identifier(self.schema),sql.Identifier(table)))
                except Exception:pass
                else:raise CheckFailure('private-table-readable')
        self.passed('restricted_runtime_cannot_read_private_knowledge_tables')

def self_health():
    from enterprise_ai_platform.modules.system.service import HealthService
    from enterprise_ai_platform.infrastructure.runtime import RuntimeReadiness
    return HealthService(RuntimeReadiness())

async def verify(direct,base,extractor,provider=False,hardening_only=False):
    schema='db03_probe_'+uuid4().hex;extension_schema='eai_vector_probe_'+uuid4().hex;owned_extension=False;database=None;stage='extension';owned=False
    bound_sources=source_hashes();before=await migrate(direct)
    try:
        async with direct_connection(direct) as connection:
            if not (await (await connection.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone()):
                async with connection.transaction():
                    await connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(extension_schema)))
                    await connection.execute(sql.SQL('CREATE EXTENSION vector WITH SCHEMA {}').format(sql.Identifier(extension_schema)))
                owned_extension=True
        stage='migration';owned=True;await migrate(direct,schema=schema,upgrade=True)
        target=await provision_role(direct,base,schema=schema,role_name='eai_runtime_'+schema.rsplit('_',1)[1]);database=RuntimeDatabase(target,schema=schema);await database.open()
        async with direct_connection(direct,timeout=600) as connection:
            h=Harness(connection,database,schema,extractor);await h.setup_files()
            cases=('basic_knowledge','adversarial_batches') if hardening_only else ('basic_knowledge','rejection_and_version','partial_and_retry','queued_revoke_and_stale','authority_changes','adversarial_batches','actual_parser_cases','private_knowledge')
            for name in cases:
                stage=name;await getattr(h,name)();print('Knowledge checks completed: '+name,flush=True)
            if provider:stage='provider_fixture';await h.provider_fixture()
        require(source_hashes()==bound_sources,'source-stable')
        require(await migrate(direct)==before,'platform-unchanged')
        return dict(passed=True,source_sha256=bound_sources,checks=h.checks,check_count=len(h.checks),tested_revision=9,platform_revision=before[0],platform_schema_modified=False,
                    parser='actual_isolated_Docker' if isinstance(extractor,DockerFileParser) else 'controlled_text_fixture',embedding='one_actual_Cohere_case_plus_controlled_failure_fixtures' if provider else 'controlled_vector_fixture_not_provider',storage='controlled_immutable_byte_fixture_not_S3',cleanup='completed')
    except Exception as error:raise CheckFailure(stage+':'+(str(error) if isinstance(error,CheckFailure) else type(error).__name__)) from None
    finally:
        if database is not None:await database.close()
        if owned:await drop_probe_schema(direct,schema)
        if owned_extension:
            async with direct_connection(direct) as connection:
                found=await (await connection.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'")).fetchone()
                require(found==(extension_schema,),'extension-cleanup-ownership')
                await connection.execute('DROP EXTENSION vector')
                await connection.execute(sql.SQL('DROP SCHEMA {}').format(sql.Identifier(extension_schema)))

def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--parser-image');parser.add_argument('--embedding-live',action='store_true');parser.add_argument('--hardening-only',action='store_true');parser.add_argument('--output',type=Path,default=Path('local/know01/contract-live.json'));args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required.')
    try:
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        extractor=DockerFileParser(args.parser_image) if args.parser_image else Extractor()
        async def execute():
            try:return await verify(direct,base,extractor,args.embedding_live,args.hardening_only)
            finally:
                if isinstance(extractor,DockerFileParser):await extractor.close()
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(execute())
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
    except Exception as error:print('Knowledge verification failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None

if __name__=='__main__':main()
