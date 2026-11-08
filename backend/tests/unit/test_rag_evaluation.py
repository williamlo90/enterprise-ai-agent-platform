"""Evaluator invariants; these are never model-quality measurements."""
import asyncio
import copy
import importlib.util
import sqlite3
from collections import Counter
from pathlib import Path
import pytest
from pydantic import ValidationError
from enterprise_ai_platform.evaluation.rag import (
    Attempt,EvaluationError,Pack,cost,preflight,report,review_hash,run_serial,strict_json,
)
from enterprise_ai_platform.evaluation.journal import BudgetExceeded,CallGuard,RunJournal

ROOT=Path(__file__).resolve().parents[3]
spec=importlib.util.spec_from_file_location('offline_eval',ROOT/'scripts/checks/check_rag_evaluation.py')
checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)

@pytest.fixture
def inputs():return checker.load_inputs()

def records(pack,config):
    observations=asyncio.run(run_serial(pack,config,checker.FixturePort(),dataset_hash=config.dataset_sha256,rubric_hash=config.rubric_sha256))
    reviews=[r.model_copy(update={'reviewer_kind':'human','reviewer':'unit-test human simulation'}) for r in checker.fixture_reviews(observations,config.rubric_sha256)]
    return observations,reviews

def summary(pack,config,observations,reviews):
    return report(pack,config,observations,reviews,dataset_hash=config.dataset_sha256,rubric_hash=config.rubric_sha256)

def test_matrix_and_translations_and_split_isolation(inputs):
    pack,_=inputs
    assert Counter(c.split for c in pack.cases)=={'development':24,'calibration':16,'acceptance':80}
    assert len({c.family_id for c in pack.cases if c.split=='acceptance'})<80
    assert all(len({c.split for c in pack.cases if c.family_id==family})==1 for family in {c.family_id for c in pack.cases})

@pytest.mark.parametrize('mutation',['timezone','continuation_budget','ineligible','duplicate_semantic','scenario_split','bad_digest'])
def test_pack_rejects_broken_gold(inputs,mutation):
    pack,_=inputs;raw=pack.model_dump();case=raw['cases'][0]
    if mutation=='timezone':case['evaluated_at']='2026-11-06T00:00:00'
    elif mutation=='continuation_budget':
        case=next(c for c in raw['cases'] if c['turns'][0]['expected']=='clarification');case['turns'][1]['question']='x'*200
    elif mutation=='ineligible':case['required_evidence_units']=[[case['forbidden_evidence_ids'][0]]]
    elif mutation=='duplicate_semantic':case['required_evidence_units']*=2
    elif mutation=='scenario_split':raw['cases'][-1]['turns']=copy.deepcopy(case['turns'])
    elif mutation=='bad_digest':raw['documents'][0]['text']+='x'
    with pytest.raises((ValueError,ValidationError)):Pack.model_validate(raw)

def test_missing_and_failed_cases_stay_in_denominator(inputs):
    pack,config=inputs;obs,reviews=records(pack,config)
    removed=obs.pop();reviews=[r for r in reviews if (r.run,r.case_id)!=(removed.run,removed.case_id)]
    result=summary(pack,config,obs,reviews)
    assert result['scheduled']==72 and result['observed']==71
    assert result['latency']['status_counts']['missing']==1
    assert not result['complete'] and not result['acceptance_allowed']

def test_fixture_never_accepts_quality_or_human_review(inputs):
    pack,config=inputs;obs,_=records(pack,config)
    result=summary(pack,config,obs,checker.fixture_reviews(obs,config.rubric_sha256))
    assert not result['complete'] and not result['acceptance_allowed']
    assert result['cost']['all_in_cost_usd'] is None
    assert result['cost']['all_in_cost_per_success_usd'] is None

@pytest.mark.parametrize('mutation',['configuration','output','review_config'])
def test_observation_and_review_fingerprints(inputs,mutation):
    pack,config=inputs;obs,reviews=records(pack,config)
    if mutation=='configuration':obs[0]=obs[0].model_copy(update={'configuration_sha256':'a'*64})
    elif mutation=='output':obs[0].outputs[0].text+=' changed'
    else:reviews[0]=reviews[0].model_copy(update={'configuration_sha256':'a'*64})
    with pytest.raises(EvaluationError):summary(pack,config,obs,reviews)

def test_critical_citation_and_disclosure_veto(inputs):
    pack,config=inputs;obs,reviews=records(pack,config)
    case=next(c for c in pack.cases if c.id==obs[0].case_id)
    obs[0].outputs[0].cited_units=[case.forbidden_evidence_ids[0]]
    obs[0].outputs[0].text=case.forbidden_disclosure[0]+'leak'
    reviews[0].output_sha256=review_hash(obs[0])
    result=summary(pack,config,obs,reviews)
    errors=next(r['critical_errors'] for r in result['cases'] if r['run']==obs[0].run and r['case_id']==case.id)
    assert {'citation_not_in_context','unauthorized_evidence','forbidden_disclosure'}<=set(errors)
    assert result['critical_veto'] and not result['acceptance_allowed']

def test_recall_uses_context_and_deduplicates_equivalents(inputs):
    pack,config=inputs;obs,reviews=records(pack,config)
    c=next(c for c in pack.cases if c.split==config.split and c.behavior=='answer' and len(c.required_evidence_units[0])==2)
    o=next(o for o in obs if o.case_id==c.id)
    o.outputs[0].final_retrieved_units=c.required_evidence_units[0];o.outputs[0].cited_units=[]
    next(r for r in reviews if (r.case_id,r.run)==(o.case_id,o.run)).output_sha256=review_hash(o)
    row=next(r for r in summary(pack,config,obs,reviews)['cases'] if (r['case_id'],r['run'])==(o.case_id,o.run))
    assert row['recall']==1

def test_clarification_without_final_answer_fails(inputs):
    pack,config=inputs;obs,reviews=records(pack,config)
    c=next(c for c in pack.cases if c.split==config.split and c.behavior=='clarification')
    o=next(o for o in obs if o.case_id==c.id);o.outputs=o.outputs[:1]
    next(r for r in reviews if (r.case_id,r.run)==(o.case_id,o.run)).output_sha256=review_hash(o)
    row=next(r for r in summary(pack,config,obs,reviews)['cases'] if (r['case_id'],r['run'])==(o.case_id,o.run))
    assert not row['behavior_match'] and not row['pass_rubric']

def test_cost_allocation_unknown_and_no_double_count(inputs):
    pack,base=inputs
    prep=Attempt(attempt_id='prep',kind='document_embedding',outcome='ok',cost_usd=3.0)
    config=base.model_copy(update={'preprocessing_attempts':[prep],'infrastructure_cost_usd':6.0})
    obs,reviews=records(pack,config);result=summary(pack,config,obs,reviews)
    assert result['cost']['all_in_cost_usd']==9
    assert sum(r['cost']['all_in_usd'] for r in result['runs'])==pytest.approx(9)
    assert sum(s['cost']['all_in_usd'] for s in result['slices'])==pytest.approx(9)
    obs[0].attempts=[Attempt(attempt_id='unknown',kind='chat',outcome='timeout',unknown_reserved_estimate_usd=.2)]
    result=summary(pack,config,obs,reviews)
    assert result['cost']['all_in_cost_usd'] is None and result['cost']['unknown_attempts']==1
    assert result['cost']['unknown_reserved_estimate_usd']==.2
    with pytest.raises(EvaluationError):cost([prep,prep])

def test_gate_metrics_cannot_hide_missing_slice_latency(inputs):
    pack,base=inputs;config=base.model_copy(update={'p95_gate_ms':1000.0,'max_error_fraction':0.0})
    obs,reviews=records(pack,config);obs[0].latency_ms=None
    result=summary(pack,config,obs,reviews)
    assert not result['latency_gate_met'] and not result['complete']

def test_provider_denied_and_calibration_has_no_circular_gates(inputs):
    pack,base=inputs;config=base.model_copy(update={'mode':'provider','split':'calibration'})
    gaps=preflight(pack,config,config.dataset_sha256,config.rubric_sha256)
    assert 'provider_executor_not_installed' in gaps and 'human_gold_review' in gaps
    assert not {'quality_gates','p95_gate_ms','cost_per_success_gate_usd','max_error_fraction'}&set(gaps)
    class Bomb:
        async def execute(self,*args):raise AssertionError('must not execute')
    with pytest.raises(EvaluationError,match='provider_executor_not_installed'):
        asyncio.run(run_serial(pack,config,Bomb(),dataset_hash=config.dataset_sha256,rubric_hash=config.rubric_sha256))

def test_runner_serial_timeout_and_safe_failure(inputs):
    pack,base=inputs;config=base.model_copy(update={'case_timeout_ms':1})
    class Port:
        active=0;maximum=0
        async def execute(self,case,run):
            self.active+=1;self.maximum=max(self.maximum,self.active)
            try:await asyncio.sleep(.01)
            finally:self.active-=1
        async def collect_attempts(self,*args):return [],False
    port=Port();obs=asyncio.run(run_serial(pack,config,port,dataset_hash=config.dataset_sha256,rubric_hash=config.rubric_sha256))
    assert len(obs)==72 and port.maximum==1 and all(o.status=='timeout' and not o.accounting_complete for o in obs)

def test_journal_reserves_durably_before_call(inputs,tmp_path):
    _,base=inputs;config=base.model_copy(update={'budget_limit_usd':1.0})
    path=tmp_path/'run.sqlite';journal=RunJournal(path,config)
    ident=journal.reserve('case',1,'chat',.7)
    assert journal.attempts('case',1)[0].cost_usd is None
    journal=RunJournal(path,config)
    with pytest.raises(BudgetExceeded):journal.reserve('case',1,'chat',.4)
    journal.settle(ident,cost_usd=.2,input_tokens=10,output_tokens=5)
    journal.settle(ident,cost_usd=.2,input_tokens=10,output_tokens=5)
    with pytest.raises(EvaluationError,match='settlement_conflict'):journal.settle(ident,cost_usd=.1)
    assert journal.reserve('case',1,'chat',.8)
    with pytest.raises(EvaluationError,match='journal_configuration_mismatch'):RunJournal(path,base)

def test_journal_does_not_mutate_unowned_database(inputs,tmp_path):
    _,config=inputs;path=tmp_path/'other.sqlite'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE owned_by_someone_else(id INTEGER)')
    before=path.read_bytes()
    with pytest.raises(EvaluationError,match='journal_not_owned'):RunJournal(path,config)
    assert path.read_bytes()==before

def test_guard_reservation_and_invalid_tokens(inputs,tmp_path):
    _,base=inputs;config=base.model_copy(update={'budget_limit_usd':.01})
    guard=CallGuard(RunJournal(tmp_path/'guard.sqlite',config),'case',1,{'chat_input':1,'chat_output':2})
    with pytest.raises(BudgetExceeded):guard.reserve('chat',10000,10000)
    with pytest.raises(EvaluationError):guard.reserve('chat',True,100)

@pytest.mark.parametrize('raw',['{"x":1,"x":2}','{"x":NaN}','{"x":Infinity}'])
def test_json_rejects_ambiguous_and_nonfinite(raw):
    with pytest.raises(EvaluationError):strict_json(raw)
