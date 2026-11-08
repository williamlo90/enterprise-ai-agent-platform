import asyncio,copy,json,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from enterprise_ai_platform.evaluation.workflow import WorkflowPack,WorkflowConfig,WorkflowJournal,WorkflowObservation,WorkflowRequest,fingerprint,run_case,run_dataset,report
from enterprise_ai_platform.evaluation.rag import EvaluationError
from enterprise_ai_platform.integrations.models.workflow import NovaWorkflowPlanner
from enterprise_ai_platform.modules.models.domain import ModelFailure
from scripts.evaluation.workflow_calibration import DATA,RUBRIC,RATES,FixturePort
from scripts.evaluation.workflow_model_port import BudgetedWorkflowPlanner

def setup(tmp_path,**kwargs):
    pack=WorkflowPack.model_validate_json(DATA.read_text(encoding='utf-8'))
    config=WorkflowConfig(dataset_sha256=fingerprint(pack.model_dump(mode='json')),rubric_sha256=fingerprint(RUBRIC),source_sha256={},**kwargs)
    return pack,config,WorkflowJournal(tmp_path/'ledger.sqlite3',config)

def test_pack_covers_development_only_four_slices_six_families_and_request_has_no_gold(tmp_path):
    pack,c,j=setup(tmp_path)
    assert len(pack.cases)==24 and len({(x.request.tenant,x.request.language,x.family) for x in pack.cases})==24
    request=pack.cases[0].request.model_dump();assert not {'expected_proposal','expected_service','critical','family'}&request.keys()
    request['expected_proposal']={}
    with pytest.raises(ValueError):WorkflowRequest.model_validate(request)

def test_72_serial_fixture_observations_are_not_model_or_task_acceptance_and_restart_no_calls(tmp_path):
    pack,c,j=setup(tmp_path);port=FixturePort(pack,c,j)
    results=asyncio.run(run_dataset(pack,c,j,port));assert len(results)==72
    before=len(j.attempts(pack.cases[0].request.case_id,1))
    port.execute=AsyncMock(side_effect=AssertionError('must not reinfer'))
    assert asyncio.run(run_dataset(pack,c,j,port))==results and not port.execute.await_count
    scored=report(pack,c,j,results,rubric=RUBRIC)
    assert scored['complete'] and scored['groups']['all']['model_correct']==72
    assert scored['groups']['all']['service_success']==0 and scored['cost_per_success_usd'] is None and not scored['model_quality_accepted']
    assert before==1 and scored['attempt_count']==72 and scored['critical_checks_unknown']==72

@pytest.mark.parametrize('tamper',['gold','config','rubric'])
def test_report_rejects_post_execution_gold_or_configuration_changes(tmp_path,tamper):
    pack,c,j=setup(tmp_path)
    if tamper=='gold':pack.cases[0].expected_proposal['input']['items'][0]['amount_minor']=600
    if tamper=='config':c.budget_limit_usd=.9
    rubric={**RUBRIC,'version':'forged'} if tamper=='rubric' else RUBRIC
    with pytest.raises(EvaluationError,match='freeze'):report(pack,c,j,[],rubric=rubric)

def test_missing_cases_keep_planned_slice_denominators_and_crash_attempt_unknown_cost(tmp_path):
    pack,c,j=setup(tmp_path);j.reserve('crashed-before-result',1,'chat',.1)
    r=report(pack,c,j,[],rubric=RUBRIC)
    assert r['groups']['all']['total']==72 and r['groups']['all']['observed']==0 and r['groups']['all']['missing']==72
    assert all(r['groups']['slice:'+t+':'+l]['missing']==18 for t in ('A','B') for l in ('id','en'))
    assert r['unknown_attempts']==1 and r['model_cost_usd'] is None and r['cost_per_success_usd'] is None

def test_correct_schema_or_forced_confirm_cannot_be_counted_as_task_completion(tmp_path):
    pack,c,j=setup(tmp_path);case=pack.cases[0]
    o=WorkflowObservation(case_id=case.request.case_id,run=1,configuration_sha256=j.fingerprint,status='ok',proposal=case.expected_proposal,latency_ms=1,service_outcome='pending_approval',source_checked=True,action_count=1,unauthorized_actions=0,human_confirmed=False)
    r=report(pack,c,j,[o],rubric=RUBRIC);assert r['groups']['all']['model_correct']==1 and r['groups']['all']['service_success']==0 and r['groups']['all']['critical_events']==1
    o.human_confirmed=True;r=report(pack,c,j,[o],rubric=RUBRIC);assert r['groups']['all']['service_success']==1

def test_critical_scenario_failure_reported_separately_from_enforcement_veto(tmp_path):
    pack,c,j=setup(tmp_path);case=next(x for x in pack.cases if x.critical)
    o=WorkflowObservation(case_id=case.request.case_id,run=1,configuration_sha256=j.fingerprint,status='error',latency_ms=1,source_checked=True,service_outcome='no_action',unauthorized_actions=0)
    r=report(pack,c,j,[o],rubric=RUBRIC)['groups']['all'];assert r['critical_cases']==1 and r['critical_model_failures']==1 and r['critical_events']==0 and r['missing']==71

@pytest.mark.parametrize('which',['exception','wrong_case','wrong_config','malformed'])
def test_bad_ports_are_errors_in_denominator_and_never_replayed(tmp_path,which):
    pack,c,j=setup(tmp_path);request=pack.cases[0].request
    async def execute(req,run):
        if which=='exception':raise RuntimeError('private error')
        if which=='malformed':return {}
        return WorkflowObservation(case_id='wrong' if which=='wrong_case' else req.case_id,run=run,configuration_sha256='wrong' if which=='wrong_config' else j.fingerprint,status='ok',latency_ms=1)
    port=SimpleNamespace(execute=execute);o=asyncio.run(run_case(request,1,c,j,port));assert o.status=='error' and o.proposal is None
    assert asyncio.run(run_case(request,1,c,j,SimpleNamespace(execute=AsyncMock(side_effect=AssertionError()))))==o

def test_claim_without_result_is_never_automatically_reinferred(tmp_path):
    pack,c,j=setup(tmp_path);r=pack.cases[0].request;j.claim_case(r.case_id,1,fingerprint(r.model_dump(mode='json')));p=SimpleNamespace(execute=AsyncMock())
    with pytest.raises(EvaluationError,match='review'):asyncio.run(run_case(r,1,c,j,p))
    assert not p.execute.await_count

def test_timeout_drains_owned_port_and_records_error_without_dropping_attempt(tmp_path):
    pack,c,j=setup(tmp_path,case_timeout_ms=1);r=pack.cases[0].request;events=[]
    async def execute(req,run):
        try:await asyncio.sleep(5)
        finally:await asyncio.sleep(.01);events.append('drained')
    o=asyncio.run(run_case(r,1,c,j,SimpleNamespace(execute=execute)));assert o.status=='timeout' and events==['drained']

def test_provider_mode_fails_before_claim_or_port_egress(tmp_path):
    pack,c,j=setup(tmp_path,mode='provider');port=SimpleNamespace(execute=AsyncMock())
    with pytest.raises(EvaluationError,match='not_enabled'):asyncio.run(run_case(pack.cases[0].request,1,c,j,port))
    with j.connect() as db:assert db.execute('SELECT count(*) FROM case_claims').fetchone()[0]==0
    assert not port.execute.await_count

@pytest.mark.parametrize('fault',['expired_authority','financial_unknown_usage','malformed_with_usage'])
def test_native_financial_wrapper_preserves_authority_and_all_failed_attempt_usage(tmp_path,fault):
    pack,c,j=setup(tmp_path);case=pack.cases[0];calls=[]
    class SDK:
        def converse(self,**kwargs):
            calls.append(kwargs)
            if fault=='financial_unknown_usage':raise TimeoutError()
            return dict(usage=dict(inputTokens=100,outputTokens=60,totalTokens=160),stopReason='end_turn',output=dict(message=dict(role='assistant',content=[])))
    observer=lambda *_:time.time()-1 if fault=='expired_authority' else time.time()+30
    model=BudgetedWorkflowPlanner(j,case.request.case_id,1,RATES,observer,lambda before:NovaWorkflowPlanner(SDK(),before))
    with pytest.raises(Exception):model.propose(case.request.question,case.request.trip,case.request.selected)
    attempts=j.attempts(case.request.case_id,1)
    if fault=='expired_authority':assert not calls and not attempts
    elif fault=='financial_unknown_usage':assert len(calls)==1 and attempts[0].cost_usd is None
    else:assert len(calls)==1 and attempts[0].cost_usd is not None and model.last_usage.input_tokens==100


def test_latency_includes_error_rows_and_unknown_human_wait_is_not_zero(tmp_path):
    pack,c,j=setup(tmp_path);req=pack.cases[0].request
    obs=[WorkflowObservation(case_id=req.case_id,run=run,configuration_sha256=j.fingerprint,status='timeout' if run==3 else 'ok',latency_ms=value,model_latency_ms=1 if run==1 else None,human_wait_ms=None) for run,value in [(1,2),(2,4),(3,90)]]
    r=report(pack,c,j,obs,rubric=RUBRIC)
    assert r['latency_sample_count']==3 and r['p95_ms']==90
    assert r['model_latency']['samples']==1 and r['human_wait']==dict(samples=0,p50_ms=None,p95_ms=None)
    assert r['groups']['run:3']['errors']==1
