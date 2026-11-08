"""New provider-capability path over actual owned app; SDK entirely controlled."""
from check_rag_dataset_execution import *
from enterprise_ai_platform.evaluation.rag import pack_content_hash
from enterprise_ai_platform.evaluation.provider import issue_provider_execution,permitted
from enterprise_ai_platform.evaluation.runner import run_case
from scripts.evaluation.provider_runtime import ProviderModels
from scripts.evaluation.rag_provider import RecordedSDK
import time
OUT=ROOT/'local/model-sel-03a/provider-entry/bridge-final'
def harness_for_gate(pack,base):
    cases=[c for c in pack.cases if c.split=='calibration' and c.behavior=='answer'];requests=[ExecutionRequest.from_case(c) for c in cases]
    units={group[0] for case in cases for group in case.required_evidence_units};documents=[d for d in pack.documents if d.unit_id in units]
    manifest=base.model_copy(update=dict(mode='provider',split='calibration',dataset_content_sha256=pack_content_hash(pack),model_id='apac.amazon.nova-pro-v1:0',embedding_model_id='cohere.embed-multilingual-v3',document_embedding_model_id='cohere.embed-multilingual-v3',region='ap-southeast-1',data_scope='fictional-only APAC exception',representative_embeddings=True,approved_by='controlled operator fixture',budget_approved_by='controlled operator fixture',provider_egress_approved=True,budget_limit_usd=1.,max_inference_attempts=148,case_timeout_ms=120000,phase_timeout_ms=900000,frozen_at='controlled freeze',price_snapshot_ref='controlled price'))
    class Harness(DatasetHarness):
        async def setup_lifecycle(self):
            self.sdk=ControlledSDK();self.cap=issue_provider_execution(manifest,scope_sha256='a'*64,gold_dataset_sha256=manifest.dataset_sha256,expires_at=time.time()+800,verify_sources=lambda:None)
            self.models=ProviderModels(manifest,self.cap,RecordedSDK(self.sdk,OUT),RATES)
            await self.prepare_execution(manifest,[ExecutionRequest.from_case(c) for c in pack.cases if c.split=='calibration'],documents,OUT,self.models.build)
        async def ingest_corpus(self):
            await super().ingest_corpus();self.models.handoff(self.manifest,self.corpus_journal,self.journal)
            require(not permitted(self.cap,manifest) and self.corpus_journal.stop_requested()=='corpus_transferred','old-corpus-permit-and-budget-retired')
        async def dataset(self):
            await self.prepare_corpus();results=[]
            for run in (1,2,3):
                for request in requests:results.append(await run_case(request,run,self.manifest,self.journal,DatasetPort(self),executor=self.models.permit))
            require(len(results)==12 and all(o.status=='ok' and len(o.outputs)==1 for o in results),'new-capability-twelve-actual-publications')
            require(len(self.sdk.calls)==len(documents)+24,'new-capability-native-sdk-count')
            self.passed('explicit_provider_capability_corpus_to_case_handoff_retires_old_ledger_and_old_capability')
            self.passed('new_provider_runner_twelve_serial_actual_gold_free_requests_graph_publications_partial_outputs_usage_known')
            self.passed('recorded_native_chat_and_byte_body_embedding_attempts_without_credentials_or_raw_prompts')
            write_json(OUT/'observations.json',[o.model_dump() for o in results]);write_json(OUT/'bridge-summary.json',dict(passed=True,provider_calls=0,controlled_sdk_calls=len(self.sdk.calls),documents=len(documents),observations=12,quality_accepted=False,old_ledger_retired=True))
    return Harness

def main():
    logging.disable(logging.CRITICAL);p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');a=p.parse_args()
    if not a.live:p.error('owned database --live only; no provider calls')
    OUT.mkdir(parents=True,exist_ok=False)
    try:
        pack,base=load_inputs();cfg=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);pooled=parse_target(cfg.database_url.get_secret_value());direct=parse_target(cfg.database_direct_url.get_secret_value(),direct=True);verify_same_database(pooled,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:value=runner.run(verify(direct,pooled,harness_type=harness_for_gate(pack,base),target_version=30,operation='dataset',direct_timeout=960))
        write_json(OUT/'database-proof.json',value);print(json.dumps({k:v for k,v in value.items() if k!='source_sha256'}))
    except Exception as e:
        value=dict(passed=False,error_type=type(e).__name__,category=str(e) if isinstance(e,CheckFailure) else 'sanitized_failure');write_json(OUT/'database-proof.json',value);print(json.dumps(value));raise SystemExit(1)
if __name__=='__main__':main()
