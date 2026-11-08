"""RAG-01 actual PostgreSQL/LangGraph with controlled model/storage fixtures."""
import argparse
import asyncio
from contextlib import contextmanager,asynccontextmanager
import hashlib
import json
import logging
from pathlib import Path
import re
from uuid import UUID,uuid4
from types import SimpleNamespace
import httpx
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.knowledge_lifecycle_check import Harness as KnowledgeHarness,Extractor
from enterprise_ai_platform.conversation_check import Harness as ConversationHarness,ControlledSources
from enterprise_ai_platform.composition import ApplicationServices,build_rag_turn,database_loop_factory
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase,direct_connection,parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import migrate
from enterprise_ai_platform.infrastructure.database.roles import provision_role,drop_probe_schema
from enterprise_ai_platform.infrastructure.database.conversations import PostgresConversationRepository
from enterprise_ai_platform.infrastructure.database.rag import PostgresRagRepository
from enterprise_ai_platform.modules.conversations.service import ConversationService
from enterprise_ai_platform.modules.rag.domain import RagFailure,context,packed,bindings
from enterprise_ai_platform.modules.rag.service import sources
from enterprise_ai_platform.modules.models.ports import EmbeddingResult,ChatReply,Message,Usage,ModelFailure
from enterprise_ai_platform.integrations.checkpoints.rag_graph import build_rag_graph
from enterprise_ai_platform.knowledge_check import source_hashes,write_json
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.file_check import ORIGIN

EN='Travel reimbursement claims must be submitted within 7 calendar days after returning.'
ID='Klaim perjalanan harus diajukan dalam 7 hari kalender setelah pulang.'

class Models:
    def __init__(self,observer,kind='answer',hook=None,before_hook=None):self.observer,self.kind,self.hook,self.before_hook=observer,kind,hook,before_hook;self.last_usage=None;self.calls=[]
    def embed(self,texts,*,input_type):
        self.last_usage=None;require(input_type=='search_query','query-type');self.observer('embedding',512)
        self.calls.append('embedding')
        if self.kind=='embedding_failure':raise ModelFailure('timeout')
        self.last_usage=Usage(12,0)
        return EmbeddingResult((tuple([1.0]+[0.0]*1023),),self.last_usage)
    def chat(self,messages,*,tools=False):
        require(tools is False and len(messages)==1,'no-tools');self.last_usage=None
        data=json.loads(messages[0].text.split('\n',1)[1])
        if self.before_hook:self.before_hook()
        self.observer('chat',4096);self.calls.append('chat')
        if self.hook:self.hook()
        self.last_usage=Usage(120,50)
        if self.kind=='failure':raise ModelFailure('timeout')
        citations=[]
        if self.kind in ('answer','source_conflict','invalid'):
            selected=sorted([v for v in data['sources'] if 'claim' in v['text'].lower() or 'klaim' in v['text'].lower()],key=lambda v:'14' not in v['text'])[:2] if self.kind=='source_conflict' else [v for v in data['sources'] if 'claim' in v['text'].lower() or 'klaim' in v['text'].lower()]
            if self.kind!='source_conflict':
                preferred=[v for v in selected if ('klaim' in v['text'].lower())==(data['language']=='id')]
                selected=preferred[:1] or selected
            citations=[dict(id=v['id'],quote=v['text']) for v in selected]
            if self.kind=='invalid':citations[0]['quote']='All claims are approved and paid.'
        value=packed(dict(kind='answer' if self.kind=='invalid' else self.kind,citations=citations))
        return ChatReply(value,None,self.last_usage,Message('assistant',value))

class Harness(KnowledgeHarness):
    create_conversation=ConversationHarness.create_conversation
    append=ConversationHarness.append
    history=ConversationHarness.history
    async def client(self):
        services=ApplicationServices(health=SimpleNamespace(),files=self.files,knowledge=self.knowledge,conversations=self.conversations)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=services)),base_url=ORIGIN)
    async def setup_rag(self):
        self.sources=ControlledSources();self.conv_repo=PostgresConversationRepository(self.database);self.conversations=ConversationService(self.conv_repo,ORIGIN,sources=self.sources)
        await self.setup_lifecycle();self.docs=[]
        for text in (EN,ID,'Parking permits are required for the office garage.','Private travel claims must be submitted within 99 days.','Travel claims expired after 55 days.'):
            row=await self.document(text);require((await self.mutate(row,'publish')).status_code==200,'publish-fixture');self.docs.append(row)
        require((await self.mutate(self.docs[3],'acl',readers=[])).status_code==200,'private-doc')
        await self.execute("UPDATE {s}.document_versions SET valid_until=(clock_timestamp() AT TIME ZONE 'Asia/Singapore')::date WHERE id=%s",(UUID(self.docs[4]['version_id']),))
        self.draft=await self.document('Unpublished travel claims require 88 days.')
        self.passed('published_acl_private_expired_and_draft_knowledge_fixtures_real_ingestion')
    async def turn(self,question='What is the travel reimbursement deadline?',*,language='en',kind='answer',cid=None,hook=None):
        cid=cid or (await self.create_conversation(index=4,language=language)).json()['id']
        response=await self.append(cid,text=question,index=4);require(response.status_code==202,'admit-rag-turn')
        claim=await self.scheduler.claim();require(claim['job_type']=='agent.resume','agent-job')
        models=[]
        @contextmanager
        def factory(observer):
            value=Models(observer,kind,hook);models.append(value);yield SimpleNamespace(chat=value,embedding=value)
        print('RAG turn admission accepted',flush=True)
        worker=await build_rag_turn(self.database,self.jobs,claim,model_factory=factory,mode='vector')
        for label,repository in (('rag',worker.repository),('checkpoint',worker.saver.repository)):
            original=repository.execute
            def wrap(function,label):
                async def diagnostic(connection,operation,payload):
                    try:return await function(connection,operation,payload)
                    except Exception as error:
                        print('RPC diagnostic '+label+':'+operation+':'+type(error).__name__+':'+str(getattr(error,'status',None))+':'+str(getattr(error,'sqlstate',None))+':'+str(re.findall(r'line [0-9]+',str(getattr(getattr(error,'diag',None),'context','')))),flush=True);raise
                return diagnostic
            repository.execute=wrap(original,label)
        print('RAG worker composed',flush=True)
        return worker,cid,models
    async def output(self,worker):
        return await self.scalar("SELECT jsonb_build_object('body',body,'kind',output_kind,'id',id) FROM {s}.messages WHERE run_id=%s AND author_kind='model'",(UUID(worker.repository.binding['run_id']),))
    async def pipeline(self):
        for language in ('en','id'):
            worker,cid,models=await self.turn('Apa tenggat klaim perjalanan?' if language=='id' else 'What is the travel reimbursement deadline?',language=language)
            result=await worker.run(build_rag_graph);row=await self.output(worker)
            require(row['kind']=='answer' and bool(re.search(r'\[S[1-5]\]',row['body'])),'grounded-answer')
            require(models[0].calls==['embedding','chat'],'bounded-provider-calls')
            count=await self.scalar("SELECT count(*) FROM {s}.rag_attempts WHERE run_id=%s AND state='known'",(UUID(worker.repository.binding['run_id']),));require(count==2,'known-usage')
            require(await self.scalar('SELECT count(*) FROM {s}.rag_citations WHERE message_id=%s',(UUID(row['id']),))==1,'citation-binding')
            raw=await self.scalar('SELECT state_payload::text FROM {s}.checkpoints WHERE run_id=%s ORDER BY checkpoint_no DESC LIMIT 1',(UUID(worker.repository.binding['run_id']),))
            require(EN not in raw and ID not in raw and 'question' not in raw,'minimal-checkpoint')
            self.sources.allowed.update((worker.repository.binding['tenant_id'],ref) for ref in sources(worker.ctx.hits))
            history=(await self.history(cid,index=4)).json()['messages'];require(history[-1]['citations'][0]['id']==worker.buffer['citations'][0]['id'],'public-citation-label')
            self.sources.allowed.clear();require((await self.history(cid,index=4)).json()['messages'][-1]['citations']==[],'citation-redaction')
        self.passed('real_graph_id_en_extractive_answer_citation_mapping_usage_checkpoint_and_redaction')
    async def compare(self):
        worker,cid,models=await self.turn()
        query='travel claims';vector=[1.0]+[0.0]*1023;results={}
        gold={self.docs[0]['version_id'],self.docs[1]['version_id']} # equivalent versions are one required evidence group
        for mode in ('vector','hybrid','rerank'):
            found=await worker.repository.call('search',dict(query=query,vector=vector,mode=mode))
            ctx=context(query,'en',found['items']);ids={v['version_id'] for v in ctx.hits}
            require(not ids.intersection({self.docs[3]['version_id'],self.docs[4]['version_id'],self.draft['version_id']}),'filter-before-ranking')
            require(gold.issubset(ids),'required-evidence')
            results[mode]=dict(required_unique_evidence_recall_at_final_top5=float(bool(ids&gold)),returned=len(ctx.hits))
        self.comparison=dict(scope='mechanism fixture; deterministic synthetic embeddings, not semantic model or holdout acceptance',results=results,selected='vector',reason='Same required evidence recall on this small fixture; keep simpler baseline until representative development evaluation demonstrates benefit')
        await worker.saver.finish('failed')
        self.passed('same_corpus_actor_query_gold_vector_hybrid_heuristic_rerank_final_context_comparison')
        self.passed('acl_effective_date_draft_filters_before_ranking_and_context')
    async def kinds(self):
        for kind in ('clarification','insufficient_evidence','source_conflict','invalid','failure','embedding_failure'):
            if kind=='source_conflict':
                conflict=await self.document('Travel reimbursement claims must be submitted within 14 calendar days after returning.')
                require((await self.mutate(conflict,'publish')).status_code==200,'conflicting-fixture')
            worker,cid,models=await self.turn(kind=kind)
            await worker.run(build_rag_graph);row=await self.output(worker)
            expected='service_unavailable' if kind in ('invalid','failure','embedding_failure') else kind
            require(row['kind']==expected,'output-kind-'+kind)
            if kind=='embedding_failure':
                require(models[0].calls==['embedding'],'embedding-failure-no-chat')
                require(await self.scalar("SELECT state FROM {s}.rag_attempts WHERE run_id=%s AND kind='embedding'",(UUID(worker.repository.binding['run_id']),))=='unknown','unknown-embedding-usage')
            if kind=='source_conflict':require((await self.mutate(conflict,'withdraw')).status_code==200,'withdraw-conflict-fixture')
            if kind=='clarification':
                next_worker,_,_=await self.turn('Travel reimbursement.',cid=cid,kind='answer')
                data=await next_worker.repository.call('input',{});require(data['prior_question'] and data['question']=='Travel reimbursement.','clarification-context')
                await next_worker.run(build_rag_graph)
        self.passed('clarification_followup_new_run_without_pending_deadlock_or_approval')
        self.passed('abstain_conflict_invalid_model_output_and_failure_are_distinct')
    async def rejected(self,call,status=None):
        try:await call
        except RagFailure as error:
            if status is not None:require(error.status==status,'expected-rejection-'+str(error.status))
        else:raise CheckFailure('unexpected-permission')

    async def boundaries(self):
        worker,_,models=await self.turn();binding=worker.repository.binding
        for change in (dict(tenant_id=str(self.people[2]['tenant'])),dict(run_id=str(uuid4())),dict(job_id=str(uuid4())),dict(generation=binding['generation']+1),dict(epoch=binding['epoch']+1)):
            bad=PostgresRagRepository(self.database,{**binding,**change})
            for operation in ('input','search','recheck','reserve','publish'):await self.rejected(bad.call(operation,{}))
        for value in (None,True,'1'):
            bad=PostgresRagRepository(self.database,{**binding,'epoch':value});await self.rejected(bad.call('input',{}),400)
        self.passed('every_rag_entrypoint_wrong_tenant_run_job_generation_epoch_and_null_authority_denied')
        values=dict(items=[],kind='embedding',request_digest='a'*64)
        await worker.repository.call('reserve',values);await self.rejected(worker.repository.call('reserve',values),403)
        await worker.repository.call('usage',dict(items=[],kind='embedding',input_tokens=None,output_tokens=None))
        await self.rejected(worker.repository.call('reserve',values),403)
        require(await self.scalar("SELECT count(*) FROM {s}.rag_attempts WHERE run_id=%s AND state='unknown'",(UUID(binding['run_id']),))==1,'unknown-no-retry')
        require(models==[],'no-model-used')
        self.passed('durable_unknown_reservation_blocks_repeat_provider_attempt_before_sdk')
        for table in ('rag_attempts','rag_citations'):
            require(await self.scalar('SELECT relrowsecurity AND relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s',(self.schema,table)),'private-rls')
            async with self.database.transaction() as connection:
                try:await connection.execute(sql.SQL('SELECT * FROM {}.{}').format(sql.Identifier(self.schema),sql.Identifier(table)))
                except Exception:pass
                else:raise CheckFailure('private-rag-readable')
        await self.execute('GRANT SELECT ON {s}.rag_attempts TO {a}')
        try:await self.rejected(worker.repository.call('input',{}),503)
        finally:await self.execute('REVOKE SELECT ON {s}.rag_attempts FROM {a}')
        await worker.saver.finish('failed')
        self.passed('rag_ledger_citations_forced_rls_private_and_grant_drift_rejected')

    async def publication_atomicity(self):
        worker,_,_=await self.turn();found=await worker.repository.call('search',dict(query='claims',vector=[1.0]+[0.0]*1023,mode='vector'));items=bindings(found['items'])
        rid=UUID(worker.repository.binding['run_id'])
        before=await self.scalar("SELECT jsonb_build_array(version,writer_generation,(SELECT count(*) FROM {s}.messages WHERE run_id=%s),(SELECT count(*) FROM {s}.source_dependencies WHERE run_id=%s)) FROM {s}.runs WHERE id=%s",(rid,rid,rid))
        # Deliberately COMMIT a non-ok raw capability result: SQL must roll back
        # its own intermediate inserts, independently of Python's error boundary.
        async with self.database.transaction() as connection:
            payload={**worker.repository.binding,'items':items,'kind':'answer','text':'Untrusted test body.','citations':[dict(id='S9',quote='bad')]}
            row=(await (await connection.execute(sql.SQL('SELECT {}.rag_call(%s,%s)').format(sql.Identifier(self.schema)),('publish',Jsonb(payload)))).fetchone())[0]
            require(row['status']=='invalid','invalid-publication')
        after=await self.scalar("SELECT jsonb_build_array(version,writer_generation,(SELECT count(*) FROM {s}.messages WHERE run_id=%s),(SELECT count(*) FROM {s}.source_dependencies WHERE run_id=%s)) FROM {s}.runs WHERE id=%s",(rid,rid,rid))
        require(before==after,'partial-publication-committed');await worker.saver.finish('failed')
        self.passed('malformed_citation_raw_rpc_commit_cannot_leave_partial_message_sources_or_run_transition')

    async def clarification_chain(self):
        cid=None
        for question in ('Which rule applies?','Travel reimbursement.','After returning.'):
            worker,cid,_=await self.turn(question,kind='clarification',cid=cid)
            data=await worker.repository.call('input',{})
            if question=='After returning.':require('Which rule applies?' in data['prior_question'] and 'Travel reimbursement.' in data['prior_question'],'initial-question-preserved')
            await worker.run(build_rag_graph)
        worker,_,models=await self.turn('Please continue.',kind='clarification',cid=cid)
        await worker.run(build_rag_graph);row=await self.output(worker)
        require(row['kind']=='insufficient_evidence' and models[0].calls==[],'clarification-limit-no-provider')
        self.passed('two_clarification_followups_keep_initial_question_and_third_round_stops_without_model_loop')

    async def heartbeat_contention(self):
        worker,_,_=await self.turn();factory=worker.saver.repository.source_factory;delayed=False;beats=[];renew=self.jobs.heartbeat
        def source_factory(connection):
            original=factory(connection)
            @asynccontextmanager
            async def hold(subject,refs):
                nonlocal delayed
                async with original.hold(subject,refs) as decisions:
                    if refs and not delayed:delayed=True;await asyncio.sleep(4.15)
                    yield decisions
            return SimpleNamespace(hold=hold)
        async def heartbeat(lease):
            result=await renew(lease);beats.append(result['status']);return result
        worker.saver.repository.source_factory=source_factory;self.jobs.heartbeat=heartbeat
        try:await worker.run(build_rag_graph)
        finally:self.jobs.heartbeat=renew
        require(delayed and beats and all(v=='ok' for v in beats),'heartbeat-coordination')
        require((await self.output(worker))['kind']=='answer','turn-survives-own-lock')
        self.passed('checkpoint_guard_over_heartbeat_interval_serializes_renewal_without_self_contention')
    async def revocation(self):
        for action in ('acl','replace_date','withdraw'):
            worker,cid,models=await self.turn();async_loop=asyncio.get_running_loop()
            def revoke():
                async def change():
                    for row in self.docs[:3]:
                        if action=='acl':require((await self.mutate(row,'acl',readers=[])).status_code==200,'revoke-acl')
                        elif action=='withdraw':require((await self.mutate(row,'withdraw')).status_code==200,'withdraw')
                        else:await self.execute("UPDATE {s}.document_versions SET valid_from=(clock_timestamp() AT TIME ZONE 'Asia/Singapore')::date+1 WHERE id=%s",(UUID(row['version_id']),))
                asyncio.run_coroutine_threadsafe(change(),async_loop).result(timeout=10)
            # Mutate after actual provider egress callback, before model response returns.
            worker.model_factory=self.hooked_factory(revoke)
            try:await worker.run(build_rag_graph)
            except Exception:pass
            else:raise CheckFailure('revoked-output-published')
            require(await self.scalar("SELECT count(*) FROM {s}.messages WHERE run_id=%s AND author_kind='model'",(UUID(worker.repository.binding['run_id']),))==0,'no-revoked-message')
            if action=='acl':
                for row in self.docs[:3]:require((await self.mutate(row,'acl',readers=[str(self.people[4]['person'])])).status_code==200,'restore-fixture-acl')
            elif action=='replace_date':
                for row in self.docs[:3]:await self.execute('UPDATE {s}.document_versions SET valid_from=NULL WHERE id=%s',(UUID(row['version_id']),))
            elif action=='withdraw':break
        self.passed('acl_withdraw_effective_date_during_provider_inflight_discard_output_before_publication')
    async def before_egress(self):
        worker,_,_=await self.turn();loop=asyncio.get_running_loop();models=[]
        def revoke():
            async def change():
                for row in self.docs[:3]:require((await self.mutate(row,'acl',readers=[])).status_code==200,'pre-egress-revoke')
            asyncio.run_coroutine_threadsafe(change(),loop).result(timeout=10)
        @contextmanager
        def factory(observer):
            value=Models(observer,before_hook=revoke);models.append(value);yield SimpleNamespace(chat=value,embedding=value)
        worker.model_factory=factory
        try:await worker.run(build_rag_graph)
        except Exception:pass
        else:raise CheckFailure('revoked-egress-permitted')
        rid=UUID(worker.repository.binding['run_id'])
        require(models[0].calls==['embedding'],'chat-sdk-must-not-run')
        require(await self.scalar("SELECT count(*) FROM {s}.rag_attempts WHERE run_id=%s AND kind='chat'",(rid,))==0,'no-chat-reservation')
        require(await self.scalar("SELECT count(*) FROM {s}.messages WHERE run_id=%s AND author_kind='model'",(rid,))==0,'no-egress-answer')
        require(await self.scalar('SELECT state FROM {s}.runs WHERE id=%s',(rid,))=='held','source-failure-held')
        for row in self.docs[:3]:require((await self.mutate(row,'acl',readers=[str(self.people[4]['person'])])).status_code==200,'restore-pre-egress-acl')
        self.passed('acl_revoke_after_context_before_provider_reservation_blocks_chat_egress_and_holds_run')
    def hooked_factory(self,hook):
        @contextmanager
        def factory(observer):
            value=Models(observer,hook=hook);yield SimpleNamespace(chat=value,embedding=value)
        return factory


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
            h=Harness(connection,database,schema,Extractor());h.direct=direct;await h.setup_rag()
            for name in ('pipeline','compare','kinds','boundaries','publication_atomicity','clarification_chain','heartbeat_contention','before_egress','revocation'):
                stage=name;await getattr(h,name)();print('RAG checks completed: '+name,flush=True)
        require(source_hashes()==sources,'source-stable');require(await migrate(direct)==before,'platform-unchanged')
        return dict(passed=True,checks=h.checks,check_count=len(h.checks),source_sha256=sources,tested_revision=13,platform_revision=before[0],platform_schema_modified=False,
          parser_embedding_storage='controlled_fixtures_no_paid_or_cloud_calls',retrieval_comparison=h.comparison,cleanup='completed')
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
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/rag01/contract-live.json'));args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required.')
    try:
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base))
        args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result);print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
    except Exception as error:print('RAG verification failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None

if __name__=='__main__':main()
