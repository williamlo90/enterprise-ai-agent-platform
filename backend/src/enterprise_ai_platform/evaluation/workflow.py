"""Development-only workflow calibration; gold never enters execution requests."""
import hashlib
import json
import time
from collections import defaultdict
from typing import Literal
from pydantic import Field,model_validator
from enterprise_ai_platform.evaluation.rag import Record,EvaluationError,configuration_hash,Attempt
from enterprise_ai_platform.evaluation.journal import RunJournal
from uuid import UUID
from datetime import date
import re
PREPARE='prepare_reimbursement'


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def fingerprint(value):return hashlib.sha256(canonical(value).encode()).hexdigest()

# Offline authoring schema is deliberately independent of application code.
# Execution adapters must still run the application's own validation/authority.
def exact(value,fields):
    if type(value) is not dict or set(value)!=set(fields):raise EvaluationError('workflow_shape')
def identifier(value):
    if type(value) is not str or str(UUID(value))!=value or UUID(value).int==0:raise EvaluationError('workflow_identifier')
def integer(value):
    if type(value) is not int or not 1<=value<2**63:raise EvaluationError('workflow_integer')
def selection(value):
    exact(value,{'kind','trip_id','claim_id','receipts'});identifier(value['trip_id'])
    if value['kind'] not in ('submission','information_update'):raise EvaluationError('workflow_selection')
    if value['kind']=='submission' and value['claim_id'] is not None:raise EvaluationError('workflow_claim')
    if value['kind']=='information_update':identifier(value['claim_id'])
    if type(value['receipts']) is not list or len(value['receipts'])>20:raise EvaluationError('workflow_receipts')
    for r in value['receipts']:exact(r,{'id','version'});identifier(r['id']);integer(r['version'])
    if len({r['id'] for r in value['receipts']})!=len(value['receipts']):raise EvaluationError('workflow_receipt_duplicates')
def source_trip(value,trip_id):
    exact(value,{'id','start_date','end_date','state','employee_ref','items'})
    if value['id']!=trip_id or value['state']!='completed' or type(value['items']) is not list or not 1<=len(value['items'])<=100:raise EvaluationError('workflow_trip')
    if date.fromisoformat(value['start_date'])>date.fromisoformat(value['end_date']):raise EvaluationError('workflow_dates')
    if type(value['employee_ref']) is not str or not 1<=len(value['employee_ref'].encode())<=128:raise EvaluationError('workflow_employee')
    for item in value['items']:
        exact(item,{'id','expense_date','category','declared_amount_minor','currency'});identifier(item['id']);integer(item['declared_amount_minor']);date.fromisoformat(item['expense_date'])
        if item['currency'] not in ('SGD','USD','IDR') or not re.fullmatch('[a-z][a-z0-9_]{0,63}',item['category']):raise EvaluationError('workflow_trip_item')
    if len({r['id'] for r in value['items']})!=len(value['items']):raise EvaluationError('workflow_item_duplicates')
def proposal(value):
    exact(value,{'name','input'})
    if value['name']=='clarify_reimbursement':
        exact(value['input'],{'reason'})
        if value['input']['reason'] not in ('missing_amount','missing_receipt','ambiguous_request'):raise EvaluationError('workflow_clarification')
    elif value['name']==PREPARE:
        exact(value['input'],{'currency','items'})
        if value['input']['currency'] not in ('SGD','USD','IDR') or type(value['input']['items']) is not list or not 1<=len(value['input']['items'])<=8:raise EvaluationError('workflow_proposal')
        for item in value['input']['items']:
            exact(item,{'trip_item_id','amount_minor','receipt_ids'});identifier(item['trip_item_id']);integer(item['amount_minor'])
            if type(item['receipt_ids']) is not list or len(item['receipt_ids'])>20:raise EvaluationError('workflow_proposal_receipts')
            for receipt in item['receipt_ids']:identifier(receipt)
    else:raise EvaluationError('workflow_tool')

class WorkflowRequest(Record):
    case_id:str
    tenant:Literal['A','B']
    language:Literal['id','en']
    question:str=Field(min_length=1,max_length=512)
    trip:dict
    selected:dict
    @model_validator(mode='after')
    def check(self):
        selection(self.selected);source_trip(self.trip,self.selected['trip_id']);return self

class WorkflowCase(Record):
    family:str
    request:WorkflowRequest
    expected_proposal:dict
    expected_service:Literal['pending_approval','no_action']
    critical:bool=False
    @model_validator(mode='after')
    def check(self):
        proposal(self.expected_proposal)
        if (self.expected_proposal['name']==PREPARE)!=(self.expected_service=='pending_approval'):raise EvaluationError('workflow_gold_mismatch')
        return self

class WorkflowPack(Record):
    schema_version:Literal[1]=1
    version:str
    split:Literal['development']='development'
    gold_review:Literal['proposed_pending_human','human_reviewed']='proposed_pending_human'
    cases:list[WorkflowCase]
    @model_validator(mode='after')
    def check(self):
        ids=[c.request.case_id for c in self.cases]
        if len(ids)!=len(set(ids)) or not ids:raise EvaluationError('workflow_case_ids')
        return self

class WorkflowConfig(Record):
    schema_version:Literal[1]=1
    mode:Literal['fixture','provider']='fixture'
    split:Literal['development']='development'
    repeats:Literal[3]=3
    concurrency:Literal[1]=1
    dataset_sha256:str
    rubric_sha256:str
    source_sha256:dict[str,str]
    model_id:Literal['apac.amazon.nova-pro-v1:0']='apac.amazon.nova-pro-v1:0'
    region:Literal['ap-southeast-1']='ap-southeast-1'
    fictional_apac:Literal[True]=True
    temperature:Literal[0]=0
    output_tokens:Literal[256]=256
    max_inference_attempts:int=Field(default=72,ge=1,le=72)
    budget_limit_usd:float=Field(default=1,gt=0,le=1)
    preprocessing_attempts:list[Attempt]=Field(default_factory=list,max_length=0)
    case_timeout_ms:int=Field(default=120000,ge=1,le=120000)
    phase_timeout_ms:int=Field(default=900000,ge=1,le=3600000)

class WorkflowObservation(Record):
    case_id:str
    run:int=Field(ge=1,le=3)
    configuration_sha256:str
    status:Literal['ok','error','timeout']
    proposal:dict|None=None
    latency_ms:float=Field(ge=0)
    model_latency_ms:float|None=Field(default=None,ge=0)
    human_wait_ms:float|None=Field(default=None,ge=0)
    service_outcome:Literal['unexecuted','pending_approval','no_action','unknown']='unexecuted'
    source_checked:bool=False
    human_confirmed:bool=False
    action_count:int=Field(default=0,ge=0)
    unauthorized_actions:int|None=Field(default=None,ge=0)
    replayed:bool=False

class WorkflowJournal(RunJournal):
    def get(self,case_id,run):
        with self.connect() as db:row=db.execute('SELECT body FROM observations WHERE case_id=? AND run=?',(case_id,run)).fetchone()
        return WorkflowObservation.model_validate_json(row[0]) if row else None

async def run_case(request,run,config,journal,port):
    from enterprise_ai_platform.evaluation.runner import drain
    import asyncio
    if config.mode!='fixture':raise EvaluationError('workflow_provider_executor_not_enabled')
    if type(run) is not int or not 1<=run<=3 or journal.fingerprint!=configuration_hash(config):raise EvaluationError('workflow_run_binding')
    claimed=journal.claim_case(request.case_id,run,fingerprint(request.model_dump(mode='json')))
    saved=journal.get(request.case_id,run)
    if saved is not None:return saved
    if not claimed:raise EvaluationError('workflow_claim_requires_review')
    began=time.perf_counter();status='error';result=None;cancelled=None
    task=asyncio.create_task(port.execute(request.model_copy(deep=True),run))
    try:
        async with asyncio.timeout(config.case_timeout_ms/1000):value=await asyncio.shield(task)
        if not isinstance(value,WorkflowObservation) or (value.case_id,value.run,value.configuration_sha256)!=(request.case_id,run,journal.fingerprint):raise EvaluationError('workflow_output_binding')
        result=value;status=result.status
    except TimeoutError:
        status='timeout';task.cancel();cancelled=await drain(task)
    except asyncio.CancelledError as error:
        task.cancel();cancelled=await drain(task) or error
    except Exception:pass
    finally:
        if not task.done():task.cancel();cancelled=await drain(task) or cancelled
        if result is None or status!=result.status:result=WorkflowObservation(case_id=request.case_id,run=run,configuration_sha256=journal.fingerprint,status=status,latency_ms=(time.perf_counter()-began)*1000)
        # Returned service observations are evidence, not replay instructions.
        result=result.model_copy(update={'latency_ms':(time.perf_counter()-began)*1000})
        journal.append(result)
    if cancelled is not None:raise cancelled
    return result

async def run_dataset(pack,config,journal,port):
    import asyncio,random
    if fingerprint(pack.model_dump(mode='json'))!=config.dataset_sha256:raise EvaluationError('workflow_dataset_changed')
    if config.mode!='fixture':raise EvaluationError('workflow_provider_executor_not_enabled')
    results=[]
    async with asyncio.timeout(config.phase_timeout_ms/1000):
        for run in (1,2,3):
            cases=list(pack.cases);random.Random(run).shuffle(cases)
            for case in cases:
                if journal.get(case.request.case_id,run) is None and journal.stop_requested():raise EvaluationError('workflow_phase_stopped')
                results.append(await run_case(case.request,run,config,journal,port))
    return results

def report(pack,config,journal,observations,*,rubric):
    if fingerprint(pack.model_dump(mode='json'))!=config.dataset_sha256 or configuration_hash(config)!=journal.fingerprint or fingerprint(rubric)!=config.rubric_sha256:raise EvaluationError('workflow_report_freeze_mismatch')
    cases={c.request.case_id:c for c in pack.cases};seen=set();groups=defaultdict(lambda:dict(total=0,observed=0,missing=0,model_correct=0,service_success=0,errors=0,critical_events=0,critical_cases=0,critical_model_failures=0));known=0.;unknown=0;success=0
    for c in pack.cases:
        for run in (1,2,3):
            for name in ('all','run:'+str(run),'slice:'+c.request.tenant+':'+c.request.language,'family:'+c.family):groups[name]['total']+=1
    latencies=[];model_latencies=[];human_waits=[];unknown_security=0
    for o in observations:
        key=(o.case_id,o.run)
        if key in seen or o.case_id not in cases or o.configuration_sha256!=journal.fingerprint:raise EvaluationError('workflow_report_binding')
        latencies.append(o.latency_ms)
        if o.model_latency_ms is not None:model_latencies.append(o.model_latency_ms)
        if o.human_wait_ms is not None:human_waits.append(o.human_wait_ms)
        unknown_security+=o.unauthorized_actions is None
        seen.add(key);c=cases[o.case_id];correct=o.status=='ok' and o.proposal==c.expected_proposal
        critical=(o.unauthorized_actions or 0)>0 or (o.action_count>0 and not o.human_confirmed) or o.action_count>1
        actual=correct and not critical and o.source_checked and o.unauthorized_actions==0 and o.service_outcome==c.expected_service and ((o.human_confirmed and o.action_count==1) if c.expected_service=='pending_approval' else o.action_count==0)
        success+=bool(actual)
        for group in ('all','run:'+str(o.run),'slice:'+c.request.tenant+':'+c.request.language,'family:'+c.family):
            g=groups[group];g['observed']+=1;g['critical_cases']+=c.critical;g['critical_model_failures']+=c.critical and not correct;g['model_correct']+=bool(correct);g['service_success']+=bool(actual);g['errors']+=o.status!='ok';g['critical_events']+=bool(critical)
    for g in groups.values():g['missing']=g['total']-g['observed']
    # Every reserved attempt belongs in cost, including a crash before any
    # observation could be published. Never make unknown spend disappear.
    with journal.connect() as db:attempts=db.execute('SELECT state,actual,reserved FROM attempts').fetchall()
    for state,actual,reserved in attempts:
        if state!='known':unknown+=1
        else:known+=float(actual)
    import math
    ordered=sorted(latencies)
    def measured(values):
        values=sorted(values);return dict(samples=len(values),p50_ms=values[math.ceil(.5*len(values))-1] if values else None,p95_ms=values[math.ceil(.95*len(values))-1] if values else None)
    return dict(scope='workflow development; not M2 acceptance',model_quality_accepted=False,reason='fixture/provider/human-gold/prerequisite gates are independent',observation_count=len(seen),expected_observations=3*len(cases),complete=len(seen)==3*len(cases),groups=dict(groups),critical_checks_unknown=unknown_security,attempt_count=len(attempts),known_model_cost_usd=known,unknown_attempts=unknown,model_cost_usd=known if unknown==0 else None,cost_per_success_usd=(known/success if success and not unknown else None),latency_sample_count=len(ordered),p50_ms=ordered[math.ceil(.5*len(ordered))-1] if ordered else None,p95_ms=ordered[math.ceil(.95*len(ordered))-1] if ordered else None,model_latency=measured(model_latencies),human_wait=measured(human_waits),infrastructure_cost_usd=None,all_in_cost_usd=None,cost_basis='controlled token-price simulation' if config.mode=='fixture' else 'provider token estimate, not invoice')
