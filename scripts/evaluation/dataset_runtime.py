"""Owned database fixtures + actual application execution, with no gold in ports.

The caller supplies a model-port builder. This module never enables a provider,
loads credentials, or calls an arbitrary model endpoint by itself.
"""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import asyncio
import time
from pathlib import Path
from uuid import UUID,uuid4,uuid5,NAMESPACE_URL
from zoneinfo import ZoneInfo

from enterprise_ai_platform.authorization_check import require
from enterprise_ai_platform.composition import build_rag_turn
from enterprise_ai_platform.evaluation.execution import ActorBinding,SourceBinding,SourceCatalog
from enterprise_ai_platform.evaluation.journal import RunJournal
from enterprise_ai_platform.evaluation.rag import configuration_hash
from enterprise_ai_platform.evaluation.runner import drain
from enterprise_ai_platform.integrations.checkpoints.rag_graph import build_rag_graph
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.modules.files.service import FileInspection
from enterprise_ai_platform.infrastructure.database.conversations import PostgresConversationRepository
from enterprise_ai_platform.modules.conversations.service import ConversationService
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.rag_check import Harness as RagHarness
from enterprise_ai_platform.knowledge_check import write_json


class DatasetHarness(RagHarness):
    async def prepare_execution(self,manifest,requests,documents,out,model_builder):
        self.manifest=manifest;self.requests=requests;self.documents=documents
        self.phase_deadline=time.monotonic()+manifest.phase_timeout_ms/1000
        self.out=Path(out);self.out.mkdir(parents=True,exist_ok=True)
        self.model_builder=model_builder;self.phase='corpus'
        self.tenant_ids={name:uuid5(NAMESPACE_URL,'eai:evaluation:tenant:'+name) for name in ['A','B']}
        self.journal=RunJournal(self.out/('corpus-'+uuid4().hex+'.sqlite'),manifest)
        self.conversations=ConversationService(PostgresConversationRepository(self.database,live_sources=True),ORIGIN)
        self.people=[None];self.publishers={};self.executors={};self.actors={};self.actor_bindings={}
        self.scheduler=await self.caller('scheduler')
        for name,tenant in self.tenant_ids.items():
            person=uuid4();member=await self.member(tenant,person,grants=(RoleGrant('employee','tenant',str(tenant)),RoleGrant('knowledge_manager','tenant',str(tenant))))
            await self.execute("UPDATE {s}.tenants SET timezone='Asia/Singapore' WHERE id=%s",(tenant,))
            token,csrf=await self.session(tenant,person)
            self.publishers[name]=dict(tenant=tenant,person=person,member=member,token=token,csrf=csrf)
            self.executors[name]=await self.caller('executor',tenant)
        for request in requests:
            if request.principal_id in self.actors:
                index=self.actors[request.principal_id];person=self.people[index]
                require(str(person['member'])==request.membership_id and person['tenant']==self.tenant_ids[request.tenant],'frozen-actor-consistency')
                self.actor_bindings[request.principal_id].verify(request,str(person['tenant']),self.actor_bindings[request.principal_id].runtime_access_revision)
                continue
            tenant=self.tenant_ids[request.tenant];person=UUID(request.principal_id)
            member=await self.member(tenant,person,UUID(request.membership_id),grants=(RoleGrant('employee','tenant',str(tenant)),))
            revision=await self.scalar('SELECT access_revision FROM {s}.memberships WHERE id=%s AND tenant_id=%s',(member,tenant))
            self.actor_bindings[request.principal_id]=ActorBinding(tenant=request.tenant,tenant_id=str(tenant),principal_id=request.principal_id,membership_id=str(member),authored_access_revision=request.access_revision,runtime_access_revision=revision)
            token,csrf=await self.session(tenant,person)
            self.actors[request.principal_id]=len(self.people)
            self.people.append(dict(tenant=tenant,person=person,member=member,token=token,csrf=csrf))
        require(all(reader in self.actors for doc in documents for reader in doc.readers),'corpus-readers-have-fixture-actors')
        self.select_publisher('A')

    def select_publisher(self,tenant):
        self.people[0]=self.publishers[tenant];self.jobs=self.executors[tenant]
        self.inspector=FileInspection(self.file_repo,self.store,self.parser,self.jobs,self.jobs._credential_hash)

    def ports(self,observer,case_id,run):return self.model_builder(self.journal,case_id,run,observer)

    async def process_knowledge(self,claim=None,extractor=None,embedding=None):
        import asyncio
        from enterprise_ai_platform.modules.knowledge.service import KnowledgeIngestion
        require(self.phase=='corpus','corpus-already-frozen')
        claim=claim or await self.scheduler.claim();require(claim.get('job_type')=='knowledge.ingest','corpus-ingestion-job')
        worker=KnowledgeIngestion(self.repo,self.store,extractor or self.extractor,self.jobs,None,self.jobs._credential_hash)
        worker.embedding=self.ports(worker.observer(asyncio.get_running_loop()),'__preprocessing__:'+self.preparing_unit,0).embedding
        return await worker.process(claim)

    @staticmethod
    def check_clock(request):
        expected=datetime.fromisoformat(request.evaluated_at).astimezone(ZoneInfo('Asia/Singapore')).date()
        require(datetime.now(ZoneInfo('Asia/Singapore')).date()==expected,'frozen-fixture-day-changed')

    async def prepare_corpus(self):
        owned=asyncio.create_task(self.ingest_corpus())
        try:
            async with asyncio.timeout(max(0,self.phase_deadline-time.monotonic())):
                return await asyncio.shield(owned)
        except BaseException:
            if not owned.done():owned.cancel()
            cancelled=await drain(owned)
            if cancelled is not None:raise cancelled
            raise

    async def ingest_corpus(self):
        for request in self.requests:self.check_clock(request)
        bindings=[];provenance=[]
        for doc in self.documents:
            self.preparing_unit=doc.unit_id
            self.select_publisher(doc.tenant)
            file_id=await self.ready_file(doc.text)
            created=await self.create_document(file_id,title=doc.unit_id,readers=doc.readers)
            require(created.status_code==202,'corpus-admission');source=created.json()
            await self.process_knowledge()
            require((await self.mutate(source,'publish')).status_code==200,'corpus-publish')
            if doc.publication_state=='withdrawn':
                require((await self.mutate(source,'withdraw')).status_code==200,'corpus-withdraw')
            # Admin maintenance below is restricted to the disposable fixture.
            # It models historical dates/states without changing application SQL
            # or weakening current runtime authority and publication guards.
            async with self.admin.transaction():
                await self.execute('UPDATE {s}.document_versions SET valid_from=%s,valid_until=%s WHERE id=%s',(doc.valid_from,doc.valid_until,UUID(source['version_id'])))
                if doc.publication_state=='superseded':
                    await self.execute('UPDATE {s}.documents SET active_version_id=NULL,version=version+1,access_revision=access_revision+1 WHERE id=%s',(UUID(source['document_id']),))
                    await self.execute("UPDATE {s}.document_versions SET publication_state='superseded' WHERE id=%s",(UUID(source['version_id']),))
            current=await self.scalar("SELECT jsonb_build_object('version_id',dv.id,'version_no',dv.version_no,'page',c.page,'chunk_no',c.chunk_no,'access_revision',d.access_revision,'index_generation',c.index_generation,'text_digest',c.text_digest,'text',c.text,'embedding_model',dv.embedding_model,'vector',c.embedding::text,'publication_state',dv.publication_state,'valid_from',dv.valid_from,'valid_until',dv.valid_until,'tenant_id',d.tenant_id) FROM {s}.documents d JOIN {s}.document_versions dv ON dv.tenant_id=d.tenant_id AND dv.document_id=d.id JOIN {s}.chunks c ON c.tenant_id=dv.tenant_id AND c.document_version_id=dv.id AND c.index_generation=dv.index_generation WHERE dv.id=%s",(UUID(source['version_id']),))
            require(current['text']==doc.text and current['text_digest']==doc.text_digest,'frozen-corpus-content')
            require(current['publication_state']==doc.publication_state and current['valid_from']==doc.valid_from and current['valid_until']==doc.valid_until,'frozen-corpus-state-period')
            require(current['tenant_id']==str(self.tenant_ids[doc.tenant]),'frozen-corpus-tenant')
            readers=await self.scalar('SELECT array_agg(principal_id::text ORDER BY principal_id::text) FROM {s}.document_acl WHERE document_id=%s',(UUID(source['document_id']),))
            require(set(readers or [])==set(doc.readers)|{str(self.people[0]['person'])},'frozen-corpus-reader-acl')
            metadata={key:current[key] for key in ['version_id','version_no','page','chunk_no','access_revision','index_generation','text_digest']}
            bindings.append(SourceBinding(unit_id=doc.unit_id,**metadata))
            provenance.append(dict(unit_id=doc.unit_id,**metadata,tenant=doc.tenant,tenant_id=current['tenant_id'],readers=doc.readers,publication_state=doc.publication_state,valid_from=doc.valid_from,valid_until=doc.valid_until,embedding_model=current['embedding_model'],vector_sha256=hashlib.sha256(current['vector'].encode()).hexdigest()))
            print(json.dumps({'phase':'corpus','prepared':len(bindings),'scheduled':len(self.documents)}),flush=True)
        self.catalog=SourceCatalog(bindings)
        require(not self.journal.stop_requested(),'corpus-budget-stop-prevents-handoff')
        self.phase='frozen'
        write_json(self.out/'corpus-provenance.json',dict(documents=provenance,actors=[b.model_dump() for b in self.actor_bindings.values()],tenants={k:str(v) for k,v in self.tenant_ids.items()},timezone='Asia/Singapore'))
        # Final run manifest includes measured preprocessing exactly once.
        # Transfer existing attempts into a fresh case ledger, then retire corpus
        # egress. Both ledgers retain evidence; the final ledger owns the budget.
        self.corpus_journal=self.journal
        preprocessing=[attempt for doc in self.documents for attempt in self.corpus_journal.attempts('__preprocessing__:'+doc.unit_id,0)]
        require(all(a.cost_usd is not None for a in preprocessing),'corpus-usage-must-be-known-before-freeze')
        self.manifest=self.manifest.model_copy(update={'preprocessing_attempts':preprocessing,'corpus_embedding_provenance_sha256':hashlib.sha256((self.out/'corpus-provenance.json').read_bytes()).hexdigest()})
        self.journal=RunJournal(self.out/('cases-'+uuid4().hex+'.sqlite'),self.manifest)
        write_json(self.out/'run-config.json',self.manifest.model_dump())
        self.passed('whole_split_corpus_actual_ingestion_acl_state_period_vector_provenance_and_preprocessing_handoff')

    async def execute_request(self,request,run):
        require(self.phase=='frozen','execution-corpus-not-frozen');self.check_clock(request)
        index=self.actors[request.principal_id];person=self.people[index]
        require(str(person['member'])==request.membership_id and person['tenant']==self.tenant_ids[request.tenant],'execution-current-actor-binding')
        revision=await self.scalar('SELECT access_revision FROM {s}.memberships WHERE id=%s AND tenant_id=%s',(person['member'],person['tenant']))
        self.actor_bindings[request.principal_id].verify(request,str(person['tenant']),revision)
        self.jobs=self.executors[request.tenant]
        created=await self.create_conversation(index=index,language=request.language)
        require(created.status_code==201,'dataset-conversation');cid=created.json()['id']
        for question in request.questions:
            admitted=await self.append(cid,text=question,index=index);require(admitted.status_code==202,'dataset-turn-admission')
            claim=await self.scheduler.claim()
            # Agent outbox ID is the admitted run ID. target_id is obtained by
            # build_rag_turn through the executor's authorized reference call;
            # it is deliberately absent from the scheduler claim DTO.
            require(claim.get('job_type')=='agent.resume' and claim['id']==admitted.json()['run_id'],'dataset-exact-agent-job')
            @contextmanager
            def factory(observer):yield self.ports(observer,request.case_id,run)
            worker=await build_rag_turn(self.database,self.jobs,claim,model_factory=factory,mode=self.manifest.retrieval_mode,prompt_version=self.manifest.prompt_ref)
            await worker.run(build_rag_graph)
            stored=await self.output(worker)
            require(stored and stored['body']==worker.buffer['text'] and stored['kind']==worker.buffer['kind'],'dataset-actual-published-output')
            state=await self.scalar('SELECT state FROM {s}.runs WHERE id=%s',(UUID(admitted.json()['run_id']),))
            require(state=='completed','dataset-published-run-completed')
            yield self.catalog.measure(worker.ctx,worker.buffer)


class DatasetPort:
    """Separate port so Harness.execute remains its actual SQL fixture method."""
    def __init__(self,runtime):self.runtime=runtime
    async def execute(self,request,run):
        try:
            async for output in self.runtime.execute_request(request,run):yield output
        except Exception as error:
            if not getattr(self,'diagnostic_recorded',False):
                import traceback
                from enterprise_ai_platform.authorization_check import CheckFailure
                frames=[dict(file=Path(f.filename).name,line=f.lineno,function=f.name) for f in traceback.extract_tb(error.__traceback__) if 'Enterprise AI Agent' in f.filename]
                print(json.dumps({'diagnostic':'case-execution','category':type(error).__name__,'check':str(error) if isinstance(error,CheckFailure) else None,'frames':frames}),flush=True)
                self.diagnostic_recorded=True
            raise
