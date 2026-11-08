"""Reviewed one-shot RAG calibration entry. Prepare never loads credentials."""
import argparse,asyncio,hashlib,json,logging,os,sys,time
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
for path in (ROOT,ROOT/'backend/src',ROOT/'scripts/evaluation',ROOT/'scripts/checks'):sys.path.insert(0,str(path))
from enterprise_ai_platform.evaluation.rag import Manifest,load_pack,strict_json,configuration_hash,pack_content_hash,report,digest
from enterprise_ai_platform.evaluation.execution import ExecutionRequest
from enterprise_ai_platform.evaluation.provider import issue_provider_execution
from enterprise_ai_platform.evaluation.runner import run_dataset,finalize_interrupted_case
from enterprise_ai_platform.evaluation.journal import RunJournal
from scripts.evaluation.provider_runtime import run_lock,ProviderModels
from scripts.evaluation.dataset_runtime import DatasetHarness,DatasetPort
from enterprise_ai_platform.knowledge_check import write_json
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.config import DatabaseProbeSettings,load_settings
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from scripts.checks.check_knowledge_workspace import verify
RATES=dict(chat_input=1.08,chat_output=4.32,embedding_input=.1,embedding_output=0.)
PRICE_URL='https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/bedrock/USD/current/bedrock.json'
PACK=ROOT/'evaluation/rag/v1'
GOLD=ROOT/'local/model-sel-03a/provider-entry/gold-review.json'

def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def fingerprint(value):return hashlib.sha256(encoded(value)).hexdigest()
def source_inventory():
    files=list((ROOT/'backend/src/enterprise_ai_platform').rglob('*.py'))+list((ROOT/'backend/src/enterprise_ai_platform').rglob('*.sql'))
    files += [ROOT/p for p in ['backend/uv.lock','scripts/evaluation/rag_provider.py','scripts/evaluation/provider_runtime.py','scripts/evaluation/dataset_runtime.py','scripts/evaluation/model_port.py','scripts/checks/check_knowledge_workspace.py','scripts/checks/check_evaluation_execution.py']]
    return {f.relative_to(ROOT).as_posix():digest(f) for f in sorted(set(files))}
def prepare(out,budget_limit=1.,prompt_version='rag-extractive-v1'):
    require(type(budget_limit) in (int,float) and 0<budget_limit<=1,'budget-limit')
    from enterprise_ai_platform.modules.rag.domain import PROMPT_VERSION,CANDIDATE_PROMPT_VERSION
    require(prompt_version in (PROMPT_VERSION,CANDIDATE_PROMPT_VERSION),'prompt-version')
    original=strict_json((PACK/'fixture-config.json').read_text(encoding='utf-8'));pack=load_pack(PACK/'pack.json');gold=strict_json(GOLD.read_text(encoding='utf-8'))
    cases=[c for c in pack.cases if c.split=='calibration'];docs=[d for d in pack.documents if d.split=='calibration'];turns=3*sum(len(c.turns) for c in cases)
    require(gold['approved_by']=='William' and gold['split']=='calibration' and gold['dataset_sha256']==digest(PACK/'pack.json') and gold['reviewed_case_ids']==sorted(c.id for c in cases),'gold-scope-not-reviewed')
    out.mkdir(parents=True,exist_ok=False)
    for name in ('pack.json','rubric.json'):(out/name).write_bytes((PACK/name).read_bytes())
    (out/'gold-review.json').write_bytes(GOLD.read_bytes())
    source=source_inventory();write_json(out/'source.json',source)
    config=Manifest.model_validate({**original,'mode':'provider','split':'calibration','model_id':'apac.amazon.nova-pro-v1:0','embedding_model_id':'cohere.embed-multilingual-v3','document_embedding_model_id':'cohere.embed-multilingual-v3','region':'ap-southeast-1','data_scope':'fictional-only APAC exception','source_revision':fingerprint(source),'dependency_lock_sha256':digest(ROOT/'backend/uv.lock'),'prompt_ref':prompt_version,'retrieval_mode':'vector','dataset_sha256':digest(out/'pack.json'),'dataset_content_sha256':pack_content_hash(pack),'rubric_sha256':digest(out/'rubric.json'),'approved_by':'William: calibration gold only','budget_approved_by':None,'frozen_at':datetime.now(timezone.utc).isoformat(),'budget_limit_usd':float(budget_limit),'max_inference_attempts':2*turns+len(docs),'case_timeout_ms':120000,'phase_timeout_ms':900000,'price_snapshot_ref':'local/model-sel-03a/pricing-snapshot.json','representative_embeddings':True,'provider_egress_approved':False,'workload':'actual owned platform30, upload/scan/ingest real Cohere, API/JOB/RAG graph and current publication; identity/storage/parser fixtures','cache_mode':'mixed'})
    write_json(out/'planned-config.json',config.model_dump())
    request=dict(task='MODEL-SEL-03A',phase='calibration16-only, not acceptance80',cases=len(cases),repeats=3,turns=turns,document_calls=len(docs),max_query_calls=turns,max_chat_calls=turns,max_total_calls=config.max_inference_attempts,target_budget_usd=float(budget_limit),nominal_reserved_model_usd=(turns*(4096*1.08+256*4.32)+((turns+len(docs))*512*.1))/1000000,hard_billing_cap=False,rates=RATES,endpoint='https://bedrock-runtime.ap-southeast-1.amazonaws.com',profile=config.model_id,embedding=config.embedding_model_id,region=config.region,routing='fictional-only APAC exception; not Singapore-only',sdk_retries=0,fallback=False,new_aws_resources=0,provider_calls_so_far=0,phase_seconds=900,case_seconds=120,config_sha256=configuration_hash(config),source_sha256=fingerprint(source),dataset_sha256=config.dataset_sha256,rubric_sha256=config.rubric_sha256,gold_review_sha256=digest(out/'gold-review.json'),pricing_snapshot_sha256=digest(ROOT/config.price_snapshot_ref))
    write_json(out/'run-request.json',request);print(json.dumps(dict(prepared=True,scope_sha256=digest(out/'run-request.json'),calls=request['max_total_calls'],reservation_usd=request['nominal_reserved_model_usd'],provider_calls=0)))

def frozen(out):
    request=strict_json((out/'run-request.json').read_text(encoding='utf-8'));config=Manifest.model_validate(strict_json((out/'planned-config.json').read_text(encoding='utf-8')));pack=load_pack(out/'pack.json');source=strict_json((out/'source.json').read_text(encoding='utf-8'))
    if request['config_sha256']!=configuration_hash(config) or request['source_sha256']!=fingerprint(source) or source!=source_inventory() or digest(out/'pack.json')!=request['dataset_sha256'] or digest(out/'rubric.json')!=request['rubric_sha256'] or digest(out/'gold-review.json')!=request['gold_review_sha256'] or digest(ROOT/config.price_snapshot_ref)!=request['pricing_snapshot_sha256']:raise ValueError('frozen_scope_changed')
    return pack,config,request

def readiness(out):
    # Read-only control plane + public pricing, no inference or IAM changes.
    import httpx
    from enterprise_ai_platform.nova_model_check import frozen_session,preflight
    session=frozen_session();info,ready=preflight(session)
    with httpx.Client(timeout=15,trust_env=False,follow_redirects=False) as client:
        response=client.get(PRICE_URL);response.raise_for_status();raw=response.content
    require(len(raw)<4000000,'pricing-size');rates=json.loads(raw)['regions']['Asia Pacific (Singapore)'];wanted={'XPEGXF3HSTPDCWRN.JRTCKXETXF.6YS6EN2CT7':.00108,'42JMX7GP83AUXPWJ.JRTCKXETXF.6YS6EN2CT7':.00432}
    seen={v['rateCode']:float(v['price']) for v in rates.values() if v['rateCode'] in wanted};ready['fresh_nova_price']=seen==wanted
    proof=dict(checked_at=datetime.now(timezone.utc).isoformat(),passed=all(ready.values()),checks=ready,pricing_sha256=hashlib.sha256(raw).hexdigest(),account_sha256=hashlib.sha256(info['account'].encode()).hexdigest(),destination_regions=info['destination_regions'],provider_calls=0,aws_mutations=0)
    write_json(out/'readiness.json',proof);require(proof['passed'],'provider-readiness');return session,proof

class RecordedSDK:
    def __init__(self,client,out):self.client,self.out=client,out
    def invoke(self,kind,method,kwargs):
        # Record an attempted egress before SDK. Raw prompts/credentials absent.
        with (self.out/'egress.jsonl').open('ab') as f:
            material={k:dict(body_sha256=hashlib.sha256(v).hexdigest(),bytes=len(v)) if isinstance(v,bytes) else v for k,v in kwargs.items()}
            f.write(encoded(dict(kind=kind,request_sha256=fingerprint(material),at=datetime.now(timezone.utc).isoformat()))+b'\n');f.flush();os.fsync(f.fileno())
        response=method(**kwargs)
        if kind=='chat':self.protocol_note(kwargs,response)
        return response
    def protocol_note(self,request,response):
        # Optional sanitized diagnostics must never alter response/usage handling.
        note=dict(stop_reason='unknown',output_json=False,kind='unknown',citation_sentence_exact=None)
        try:
            import re
            stop=response.get('stopReason')
            if stop in ('end_turn','tool_use','max_tokens','stop_sequence','guardrail_intervened','content_filtered'):note['stop_reason']=stop
            raw=response['output']['message']['content'][0]['text'];value=json.loads(raw)
            note['output_json']=True
            if value.get('kind') in ('answer','clarification','insufficient_evidence','source_conflict'):note['kind']=value['kind']
            text=request['messages'][0]['content'][0]['text'];payload=json.loads(text if text.startswith('{') else text.split('\n',1)[1]);sources={s['id']:s['text'] for s in payload['sources']}
            citations=value['citations']
            if type(citations) is list and len(citations)<=2:note['citation_sentence_exact']=[type(c) is dict and c.get('id') in sources and c.get('quote') in re.split(r'(?<=[.!?])\s+',sources[c['id']]) for c in citations]
        except Exception:pass
        try:
            with (self.out/'protocol-notes.jsonl').open('ab') as f:f.write(encoded(note)+b'\n');f.flush();os.fsync(f.fileno())
        except OSError:pass
    def converse(self,**kwargs):return self.invoke('chat',self.client.converse,kwargs)
    def invoke_model(self,**kwargs):return self.invoke('embedding',self.client.invoke_model,kwargs)

def harness_for(pack,manifest,out,models,request_hash):
    requests=[ExecutionRequest.from_case(c) for c in pack.cases if c.split=='calibration'];documents=[d for d in pack.documents if d.split=='calibration']
    class Harness(DatasetHarness):
        def persist_state(self):
            write_json(out/'phase-state.json',dict(phase=self.phase,journal=self.journal.path.name,config=self.manifest.model_dump(),scope_sha256=request_hash))
        async def setup_lifecycle(self):
            await self.prepare_execution(manifest,requests,documents,out,models.build);self.persist_state()
        async def ingest_corpus(self):
            await super().ingest_corpus();models.handoff(self.manifest,self.corpus_journal,self.journal);self.persist_state()
        async def dataset(self):
            try:
                await self.prepare_corpus()
                await run_dataset(pack,self.manifest,self.journal,DatasetPort(self),dataset_hash=manifest.dataset_sha256,rubric_hash=manifest.rubric_sha256,phase_deadline=self.phase_deadline,executor=models.permit)
            finally:
                saved=[o for run in (1,2,3) for r in requests if (o:=self.journal.get(r.case_id,run)) is not None] if self.phase=='frozen' else []
                write_json(out/'observations.json',[o.model_dump() for o in saved])
                if self.phase=='frozen':write_json(out/'report.json',report(pack,self.manifest,saved,[],dataset_hash=manifest.dataset_sha256,rubric_hash=manifest.rubric_sha256,executor=models.permit))
                write_json(out/'phase-state.json',dict(phase=self.phase,journal=self.journal.path.name,config=self.manifest.model_dump(),scope_sha256=request_hash,paid_egress_attempts=len((out/'egress.jsonl').read_text().splitlines()) if (out/'egress.jsonl').exists() else 0))
            self.passed('actual_calibration_completed_records_persisted_not_acceptance_or_human_adjudication')
    return Harness

def provider_evidence(value,out):
    result={k:v for k,v in value.items() if k not in ('actual_paid_inference_calls','ingestion')}
    path=out/'egress.jsonl';attempts=[json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    result.update(execution_mode='provider',attempted_sdk_calls=len(attempts),storage_parser='controlled fixtures',embedding='actual Cohere provider; see attempt log and usage ledger')
    return result

def run(out,approval_hash,live):
    require(live,'explicit-live-required')
    with run_lock(out):
        pack,planned,request=frozen(out);scope=digest(out/'run-request.json');approval=strict_json((out/'budget-approval.json').read_text(encoding='utf-8'))
        require(digest(out/'budget-approval.json')==approval_hash and approval['scope_sha256']==scope and approval['approved_by']=='William' and type(approval['target_budget_usd']) in (int,float) and approval['target_budget_usd']==planned.budget_limit_usd and approval['fictional_apac'] is True,'budget-egress-approval-mismatch')
        require(not (out/'once.claim').exists(),'run-already-claimed-no-paid-replay')
        session,ready=readiness(out);frozen(out)
        manifest=planned.model_copy(update={'budget_approved_by':'William: '+approval_hash,'provider_egress_approved':True})
        def source_check():frozen(out)
        permit=issue_provider_execution(manifest,scope_sha256=scope,gold_dataset_sha256=planned.dataset_sha256,expires_at=time.time()+900,verify_sources=source_check)
        with (out/'once.claim').open('x') as f:f.write(scope)
        from enterprise_ai_platform.integrations.models.nova import NOVA_SDK_CONFIG
        permit.require(manifest)
        runtime=session.client('bedrock-runtime',region_name='ap-southeast-1',endpoint_url='https://bedrock-runtime.ap-southeast-1.amazonaws.com',config=NOVA_SDK_CONFIG)
        models=ProviderModels(manifest,permit,RecordedSDK(runtime,out),RATES);result={'passed':False}
        try:
            cfg=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);pooled=parse_target(cfg.database_url.get_secret_value());direct=parse_target(cfg.database_direct_url.get_secret_value(),direct=True);verify_same_database(pooled,direct)
            with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,pooled,harness_type=harness_for(pack,manifest,out,models,scope),target_version=30,operation='dataset',direct_timeout=1080))
        finally:
            runtime.close();result=provider_evidence(result,out);write_json(out/'database-proof.json',result)
        print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))

def recover(out,reason):
    with run_lock(out):
        pack,_,_=frozen(out);state=strict_json((out/'phase-state.json').read_text(encoding='utf-8'));manifest=Manifest.model_validate(state['config']);journal=RunJournal(out/state['journal'],manifest);recovered=[]
        for case in pack.cases:
            if case.split!='calibration':continue
            request=ExecutionRequest.from_case(case)
            for run in (1,2,3):
                with journal.connect() as db:exists=db.execute('SELECT 1 FROM case_claims WHERE case_id=? AND run=?',(case.id,run)).fetchone()
                if exists and journal.get(case.id,run) is None:recovered.append(finalize_interrupted_case(request,run,manifest,journal,reason=reason).model_dump())
        write_json(out/'recovery.json',dict(provider_calls=0,recovered=recovered,reason=reason));print(json.dumps(dict(recovered=len(recovered),provider_calls=0)))

def main():
    logging.disable(logging.CRITICAL);p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['prepare','preflight','run','recover']);p.add_argument('--out',type=Path,required=True);p.add_argument('--live',action='store_true');p.add_argument('--approval-sha256');p.add_argument('--budget-limit-usd',type=float,default=1.);p.add_argument('--prompt-version',choices=['rag-extractive-v1','rag-extractive-v3'],default='rag-extractive-v1');p.add_argument('--reason',choices=['process_interrupted','operator_reviewed_timeout'],default='process_interrupted');a=p.parse_args()
    try:
        if a.command=='prepare':prepare(a.out,a.budget_limit_usd,a.prompt_version)
        elif a.command=='preflight':frozen(a.out);_,value=readiness(a.out);print(json.dumps(value))
        elif a.command=='run':run(a.out,a.approval_sha256,a.live)
        else:recover(a.out,a.reason)
    except Exception as e:
        print(json.dumps(dict(passed=False,error_type=type(e).__name__,category=str(e) if isinstance(e,(CheckFailure,)) else 'sanitized_failure')));raise SystemExit(1) from None
if __name__=='__main__':main()
