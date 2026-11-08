"""One actual DB/API/ingestion/RAG journey with controlled Nova/Cohere SDK."""
import argparse
import asyncio
from contextlib import contextmanager
import io
import json
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/evaluation'))
from model_port import BudgetedModels
from check_knowledge_workspace import verify
from enterprise_ai_platform.authorization_check import CheckFailure,require
from enterprise_ai_platform.composition import build_rag_turn,database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings,load_settings
from enterprise_ai_platform.evaluation.execution import ExecutionRequest,SourceBinding,SourceCatalog
from enterprise_ai_platform.evaluation.journal import RunJournal
from enterprise_ai_platform.evaluation.rag import Manifest,strict_json
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.conversations import PostgresConversationRepository
from enterprise_ai_platform.integrations.checkpoints.rag_graph import build_rag_graph
from enterprise_ai_platform.integrations.models.nova import NovaChat,CohereEmbedding
from enterprise_ai_platform.knowledge_check import write_json
from enterprise_ai_platform.modules.knowledge.service import KnowledgeIngestion
from enterprise_ai_platform.modules.conversations.service import ConversationService
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.rag_check import Harness as RagHarness

OUT=ROOT/'local/model-sel-03a'
RATES={'chat_input':1.08,'chat_output':4.32,'embedding_input':.1,'embedding_output':0}

class ControlledSDK:
    def __init__(self):self.calls=[]
    def converse(self,**request):
        self.calls.append('chat')
        payload=json.loads(request['messages'][0]['content'][0]['text'].split('\n',1)[1])
        # Read only actual prompt/context; never inspect a gold Case or expected kind.
        first=payload['sources'][0]
        reply=json.dumps({'kind':'answer','citations':[{'id':first['id'],'quote':first['text']}]})
        return {'output':{'message':{'role':'assistant','content':[{'text':reply}]}},'stopReason':'end_turn','usage':{'inputTokens':120,'outputTokens':50,'totalTokens':170}}
    def invoke_model(self,**request):
        body=json.loads(request['body']);self.calls.append(body['input_type'])
        vectors=[[1.0]+[0.0]*1023 for _ in body['texts']]
        return {'body':io.BytesIO(json.dumps({'embeddings':vectors,'texts':body['texts']}).encode()),'ResponseMetadata':{'HTTPHeaders':{'x-amzn-bedrock-input-token-count':'12'}}}

class Harness(RagHarness):
    async def setup_lifecycle(self):
        self.conversations=ConversationService(PostgresConversationRepository(self.database,live_sources=True),ORIGIN)
        await super().setup_lifecycle()

    def ports(self,observer,case_id,run):
        def factory(before):return SimpleNamespace(chat=NovaChat(self.sdk,before),embedding=CohereEmbedding(self.sdk,before))
        return BudgetedModels(self.journal,case_id,run,RATES,observer,factory)

    async def process_knowledge(self,claim=None,extractor=None,embedding=None):
        claim=claim or await self.scheduler.claim()
        require(claim.get('job_type')=='knowledge.ingest','evaluation-ingestion-job')
        worker=KnowledgeIngestion(self.repo,self.store,extractor or self.extractor,self.jobs,None,self.jobs._credential_hash)
        worker.embedding=self.ports(worker.observer(asyncio.get_running_loop()),'__preprocessing__',0).embedding
        return await worker.process(claim)

    async def execution_bridge(self):
        config=Manifest.model_validate(strict_json((ROOT/'evaluation/rag/v1/fixture-config.json').read_text(encoding='utf-8')))
        config=config.model_copy(update={'workload':'one actual application path; controlled SDK only','budget_limit_usd':1.0})
        # Exclusive fresh ledger per proof; never overwrite/replay an existing run.
        from uuid import uuid4
        self.journal=RunJournal(OUT/('fixture-ledger-'+uuid4().hex+'.sqlite'),config)
        self.sdk=ControlledSDK()
        source=await self.document('Travel reimbursement claims must be submitted within 7 calendar days after returning.')
        require((await self.mutate(source,'publish')).status_code==200,'evaluation-publish')
        self.passed('actual_upload_inspection_ingestion_with_document_embedding_observer_and_journal')
        person=self.people[4]
        request=ExecutionRequest(case_id='bridge-smoke',tenant='A',principal_id=str(person['person']),membership_id=str(person['member']),access_revision=1,language='en',evaluated_at='2026-11-06T00:00:00+00:00',questions=['What is the travel reimbursement deadline?'])
        row=await self.scalar("SELECT jsonb_build_object('unit_id','bridge-source','version_id',dv.id,'version_no',dv.version_no,'page',c.page,'chunk_no',c.chunk_no,'access_revision',d.access_revision,'index_generation',c.index_generation,'text_digest',c.text_digest) FROM {s}.document_versions dv JOIN {s}.documents d ON d.tenant_id=dv.tenant_id AND d.id=dv.document_id JOIN {s}.chunks c ON c.tenant_id=dv.tenant_id AND c.document_version_id=dv.id AND c.index_generation=dv.index_generation WHERE dv.id=%s",(UUID(source['version_id']),))
        catalog=SourceCatalog([SourceBinding.model_validate(row)])
        require(str(person['person'])==request.principal_id and str(person['member'])==request.membership_id,'execution-actor-mapping')
        created=await self.create_conversation(index=4,language=request.language)
        require(created.status_code==201,'evaluation-conversation');cid=created.json()['id']
        admitted=await self.append(cid,text=request.questions[0],index=4)
        require(admitted.status_code==202,'evaluation-admit');claim=await self.scheduler.claim()
        require(claim.get('job_type')=='agent.resume','evaluation-agent-job')
        @contextmanager
        def factory(observer):yield self.ports(observer,request.case_id,1)
        worker=await build_rag_turn(self.database,self.jobs,claim,model_factory=factory)
        await worker.run(build_rag_graph)
        stored=await self.output(worker)
        require(stored['body']==worker.buffer['text'] and stored['kind']==worker.buffer['kind'],'measure-only-actual-published-output')
        measured=catalog.measure(worker.ctx,worker.buffer)
        require(measured.final_retrieved_units==measured.cited_units==['bridge-source'],'actual-ctx-and-citation-provenance')
        self.passed('gold_free_request_actual_admission_graph_publish_and_frozen_context_source_mapping')
        attempts=self.journal.attempts('__preprocessing__',0)+self.journal.attempts(request.case_id,1)
        require([a.kind for a in attempts]==['document_embedding','query_embedding','chat'],'document-query-chat-accounting')
        require(all(a.cost_usd is not None and a.outcome=='ok' for a in attempts),'known-fixture-usage')
        require(self.sdk.calls==['search_document','search_query','chat'],'serial-bounded-sdk-calls')
        require(await self.scalar("SELECT count(*) FROM {s}.rag_attempts WHERE state='known'")==2,'actual-app-budget-reservations-retained')
        self.passed('application_authority_and_financial_journal_both_retained_three_controlled_sdk_calls')
        write_json(OUT/'application-measurement.json',{'scope':'one actual application path; deterministic controlled SDK; not model quality','request':request.model_dump(),'output':measured.model_dump(),'attempts':[a.model_dump() for a in attempts],'actual_paid_calls':0,'model_quality_accepted':False})

def main():
    logging.disable(logging.CRITICAL)
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--live',action='store_true');args=p.parse_args()
    if not args.live:p.error('--live required for owned existing test DB fixture; no provider calls.')
    OUT.mkdir(parents=True,exist_ok=True)
    try:
        cfg=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(cfg.database_url.get_secret_value());direct=parse_target(cfg.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            result=runner.run(verify(direct,base,harness_type=Harness,target_version=20,operation='execution_bridge'))
        write_json(OUT/'application-proof.json',result)
        print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
    except Exception as error:
        print(json.dumps({'passed':False,'category':str(error) if isinstance(error,CheckFailure) else type(error).__name__,'provider_calls':0}))
        raise SystemExit(1) from None

if __name__=='__main__':main()
