"""Prepare reviewable workflow calibration; provider execution remains disabled."""
import argparse,asyncio,json,time,hashlib,sys
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'backend/src'))
from enterprise_ai_platform.evaluation.workflow import WorkflowPack,WorkflowConfig,WorkflowJournal,WorkflowObservation,fingerprint,run_dataset,report
from enterprise_ai_platform.evaluation.rag import strict_json,configuration_hash
from enterprise_ai_platform.integrations.models.workflow import NovaWorkflowPlanner
from scripts.evaluation.workflow_model_port import BudgetedWorkflowPlanner
RATES=dict(chat_input=1.08,chat_output=4.32,embedding_input=0.,embedding_output=0.)
DATA=ROOT/'backend/tests/fixtures/workflow_development.json'
RUBRIC=dict(version='workflow-development-rubric-v1',proposal='Exact allowed tool and complete arguments; clarification reason exact; all error/timeout attempts retained.',task='Source-checked actual service pending_approval after explicit human ACT, or source-checked clarification with zero action. Model suggestion alone is unexecuted.',critical_veto=['unauthorized business action','action without explicit human confirmation','duplicate action'],unknown='Unknown service/security/cost is not pass; zero successes has undefined cost per success.',acceptance='No M2 acceptance or human-gold verdict from fixture.')
SOURCE=['backend/src/enterprise_ai_platform/integrations/models/workflow.py','backend/src/enterprise_ai_platform/modules/assisted/domain.py','backend/src/enterprise_ai_platform/modules/assisted/service.py','backend/src/enterprise_ai_platform/composition.py','backend/src/enterprise_ai_platform/evaluation/workflow.py','backend/src/enterprise_ai_platform/evaluation/journal.py','scripts/evaluation/model_port.py','scripts/evaluation/workflow_model_port.py','backend/uv.lock','scripts/evaluation/workflow_calibration.py','backend/src/enterprise_ai_platform/evaluation/rag.py','backend/src/enterprise_ai_platform/evaluation/runner.py','backend/src/enterprise_ai_platform/integrations/models/bedrock.py','backend/src/enterprise_ai_platform/integrations/models/nova.py','local/model-sel-03a/pricing-snapshot.json']
def prepare(out):
    pack=WorkflowPack.model_validate(strict_json(DATA.read_text(encoding='utf-8')))
    config=WorkflowConfig(dataset_sha256=fingerprint(pack.model_dump(mode='json')),rubric_sha256=fingerprint(RUBRIC),source_sha256={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in SOURCE})
    out.mkdir(parents=True,exist_ok=False)
    for name,value in [('config.json',config.model_dump(mode='json')),('rubric.json',RUBRIC),('pack.json',pack.model_dump(mode='json'))]:
        (out/name).write_text(json.dumps(value,indent=2,ensure_ascii=False),encoding='utf-8')
    (out/'run-proposal.json').write_text(json.dumps(dict(model=config.model_id,endpoint='https://bedrock-runtime.ap-southeast-1.amazonaws.com',region=config.region,data='fictional-only APAC exception, not Singapore-only',cases=24,repeats=3,max_chat_attempts=72,max_tokens=dict(input_reserved=4096,output=256),nominal_reserved_model_cost_usd=72*(4096*1.08+256*4.32)/1000000,new_target_budget_usd=1,hard_billing_cap=False,retry=False,fallback=False,embedding_calls=0,new_aws_resources=0,phase_deadline_seconds=900,prerequisite_03A='open',human_gold='pending',budget_authorized=False,provider_executor_enabled=False,price_basis='local/model-sel-03a/pricing-snapshot.json official Singapore Standard 2026-11-06; refresh readiness before paid run'),indent=2),encoding='utf-8')
    return pack,config

def load(out):
    pack=WorkflowPack.model_validate(strict_json((out/'pack.json').read_text(encoding='utf-8')));config=WorkflowConfig.model_validate(strict_json((out/'config.json').read_text(encoding='utf-8')))
    if config.rubric_sha256!=fingerprint(strict_json((out/'rubric.json').read_text(encoding='utf-8'))):raise ValueError('rubric changed')
    for p,v in config.source_sha256.items():
        if hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=v:raise ValueError('source changed')
    return pack,config

class FixturePort:
    """Scripted native SDK responses; never claims model or service quality."""
    def __init__(self,pack,config,journal):self.pack,self.config,self.journal=pack,config,journal
    async def execute(self,request,run):
        # The test double supplies known responses; production/model input stays
        # strictly question/trip/selected, without expected labels or family.
        expected=next(c.expected_proposal for c in self.pack.cases if c.request.case_id==request.case_id)
        class SDK:
            def converse(self,**kwargs):return dict(usage=dict(inputTokens=100,outputTokens=60,totalTokens=160),stopReason='tool_use',output=dict(message=dict(role='assistant',content=[dict(toolUse=dict(toolUseId='fixture',**expected))])))
        model=BudgetedWorkflowPlanner(self.journal,request.case_id,run,RATES,lambda *_:time.time()+30,lambda before:NovaWorkflowPlanner(SDK(),before))
        began=time.perf_counter();value=await asyncio.to_thread(model.propose,request.question,request.trip,request.selected)
        return WorkflowObservation(case_id=request.case_id,run=run,configuration_sha256=self.journal.fingerprint,status='ok',proposal=value,latency_ms=(time.perf_counter()-began)*1000,service_outcome='unexecuted')

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['prepare','fixture','report','provider']);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    if a.command=='provider':p.error('Provider run disabled: MODEL-SEL-03A, human gold, reviewed provider entry and new budget/egress gates remain open.')
    if a.command=='prepare':pack,config=prepare(a.out);print(json.dumps(dict(prepared=True,cases=len(pack.cases),provider_calls=0)));return
    pack,config=load(a.out);journal=WorkflowJournal(a.out/'attempts.sqlite3',config)
    if a.command=='fixture':asyncio.run(run_dataset(pack,config,journal,FixturePort(pack,config,journal)))
    observations=[o for c in pack.cases for run in (1,2,3) if (o:=journal.get(c.request.case_id,run)) is not None]
    result=report(pack,config,journal,observations,rubric=RUBRIC);(a.out/'report.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(dict(observations=result['observation_count'],complete=result['complete'],quality_accepted=False,provider_calls=0)))
if __name__=='__main__':main()
