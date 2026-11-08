"""Whole-split actual application journey, controlled SDK only, serial3 runs."""
import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/evaluation'))
from dataset_runtime import DatasetHarness,DatasetPort
from model_port import BudgetedModels
from check_evaluation_execution import ControlledSDK,RATES
from check_knowledge_workspace import verify
from check_rag_evaluation import load_inputs
from enterprise_ai_platform.authorization_check import CheckFailure,require
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings,load_settings
from enterprise_ai_platform.evaluation.execution import ExecutionRequest
from enterprise_ai_platform.evaluation.rag import report
from enterprise_ai_platform.evaluation.runner import run_dataset
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.integrations.models.nova import NovaChat,CohereEmbedding
from enterprise_ai_platform.knowledge_check import write_json
from types import SimpleNamespace

OUT=ROOT/'local/model-sel-03a/dataset'

def harness_for(pack,base,split):
    requests=[ExecutionRequest.from_case(c) for c in pack.cases if c.split==split]
    documents=[d for d in pack.documents if d.split==split]
    turns=sum(len(r.questions) for r in requests)*3
    manifest=base.model_copy(update={'split':split,'case_timeout_ms':120000,'phase_timeout_ms':900000,'max_inference_attempts':2*turns+len(documents),'budget_limit_usd':1.0,'workload':'actual app corpus, authority, ingestion and graph; deterministic controlled SDK only'})
    class Harness(DatasetHarness):
        async def setup_lifecycle(self):
            self.sdk=ControlledSDK()
            def builder(journal,case_id,run,authority):
                def factory(before):return SimpleNamespace(chat=NovaChat(self.sdk,before),embedding=CohereEmbedding(self.sdk,before))
                return BudgetedModels(journal,case_id,run,RATES,authority,factory)
            try:await self.prepare_execution(manifest,requests,documents,OUT,builder)
            except Exception as error:
                import traceback
                frames=[dict(file=Path(f.filename).name,line=f.lineno,function=f.name) for f in traceback.extract_tb(error.__traceback__) if 'Enterprise AI Agent' in f.filename]
                print(json.dumps({'diagnostic':'fixture-setup','category':type(error).__name__,'frames':frames}),flush=True)
                raise
        async def dataset(self):
            await self.prepare_corpus()
            observations=[]
            try:
                observations=await run_dataset(pack,self.manifest,self.journal,DatasetPort(self),dataset_hash=base.dataset_sha256,rubric_hash=base.rubric_sha256,phase_deadline=self.phase_deadline)
            finally:
                # Retain completed/interrupted records even if the phase stops.
                saved=[row for run in range(1,4) for request in requests if (row:=self.journal.get(request.case_id,run)) is not None]
                write_json(OUT/'observations.json',[o.model_dump() for o in saved])
                result=report(pack,self.manifest,saved,[],dataset_hash=base.dataset_sha256,rubric_hash=base.rubric_sha256)
                write_json(OUT/'report.json',result)
            require(len(observations)==len(requests)*3,'whole-split-scheduled-denominator')
            require(all(o.status=='ok' and len(o.outputs)==len(next(r.questions for r in requests if r.case_id==o.case_id)) for o in observations),'whole-split-actual-publications')
            require(not result['acceptance_allowed'] and all(not r['human_review_complete'] for r in result['cases']),'fixture-never-human-or-model-quality')
            require(len(self.manifest.preprocessing_attempts)==len(documents),'all-document-embedding-attempts-counted-once')
            self.passed('all_split_cases_three_serial_runs_actual_admission_graph_publication_and_source_measurement')
            self.passed('all_case_results_durable_and_no_human_or_provider_quality_acceptance')
            self.passed('preprocessing_cost_once_final_phase_budget_and_all_attempts_accounted')
            write_json(OUT/'dataset-summary.json',{'passed':True,'mode':'fixture','scope':'actual whole-split application plumbing with deterministic SDK; not semantic/model quality','split':split,'cases':len(requests),'documents':len(documents),'observations':len(observations),'status_counts':result['latency']['status_counts'],'preprocessing_attempts':len(self.manifest.preprocessing_attempts),'controlled_sdk_calls':len(self.sdk.calls),'provider_calls':0,'paid_calls':0,'acceptance_allowed':False,'human_gold_review':'pending','corpus_provenance_sha256':hashlib.sha256((OUT/'corpus-provenance.json').read_bytes()).hexdigest()})
    return Harness

def main():
    logging.disable(logging.CRITICAL)
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--live',action='store_true');p.add_argument('--split',choices=['development','calibration','acceptance'],default='calibration');args=p.parse_args()
    if not args.live:p.error('Explicit --live required for owned existing test database; no provider calls.')
    OUT.mkdir(parents=True,exist_ok=True)
    try:
        pack,base=load_inputs()
        cfg=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings)
        pooled=parse_target(cfg.database_url.get_secret_value());direct=parse_target(cfg.database_direct_url.get_secret_value(),direct=True);verify_same_database(pooled,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            result=runner.run(verify(direct,pooled,harness_type=harness_for(pack,base,args.split),target_version=20,operation='dataset',direct_timeout=960))
        write_json(OUT/'database-proof.json',result)
        print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
    except Exception as error:
        print(json.dumps({'passed':False,'category':str(error) if isinstance(error,CheckFailure) else type(error).__name__,'provider_calls':0}))
        raise SystemExit(1) from None

if __name__=='__main__':main()
