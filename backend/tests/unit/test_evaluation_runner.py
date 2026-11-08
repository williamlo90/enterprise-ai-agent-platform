import asyncio
from pathlib import Path
import pytest
from enterprise_ai_platform.evaluation.execution import ExecutionRequest
from enterprise_ai_platform.evaluation.journal import RunJournal
from enterprise_ai_platform.evaluation.rag import EvaluationError,Manifest,Output,load_pack,strict_json
from enterprise_ai_platform.evaluation.runner import run_case,run_dataset

ROOT=Path(__file__).resolve().parents[3]

@pytest.fixture
def state(tmp_path):
    pack=load_pack(ROOT/'evaluation/rag/v1/pack.json')
    manifest=Manifest.model_validate(strict_json((ROOT/'evaluation/rag/v1/fixture-config.json').read_text(encoding='utf-8')))
    journal=RunJournal(tmp_path/'journal.sqlite',manifest)
    request=ExecutionRequest.from_case(next(c for c in pack.cases if c.split==manifest.split and len(c.turns)==2))
    return pack,manifest,journal,request

def output():return Output(kind='clarification',text='Which policy?',final_retrieved_units=[],cited_units=[])

def test_partial_outputs_and_attempts_survive_failure(state):
    _,manifest,journal,request=state
    class Port:
        async def execute(self,view,run):
            assert isinstance(view,ExecutionRequest) and not hasattr(view,'turns')
            journal.reserve(view.case_id,run,'chat',.01)
            yield output()
            raise ValueError('sensitive error is not persisted')
    observation=asyncio.run(run_case(request,1,manifest,journal,Port()))
    assert observation.status=='error' and len(observation.outputs)==1
    assert observation.attempts[0].cost_usd is None and not observation.accounting_complete
    assert 'sensitive' not in observation.model_dump_json()
    assert journal.get(request.case_id,1)==observation

def test_resume_uses_durable_result_without_reexecution(state):
    _,manifest,journal,request=state
    class Port:
        calls=0
        async def execute(self,view,run):
            self.calls+=1
            for _ in view.questions:yield output()
    port=Port();first=asyncio.run(run_case(request,1,manifest,journal,port))
    second=asyncio.run(run_case(request,1,manifest,journal,port))
    assert first==second and port.calls==1
    changed=request.model_copy(update={'questions':['Changed?']})
    with pytest.raises(EvaluationError,match='case_request_changed'):asyncio.run(run_case(changed,1,manifest,journal,port))

def test_timeout_drains_owned_execution_before_persisting(state):
    _,base,original,request=state
    manifest=base.model_copy(update={'case_timeout_ms':1})
    journal=RunJournal(Path(str(original.path)+'.timeout'),manifest)
    class Port:
        drained=False
        async def execute(self,view,run):
            attempt=journal.reserve(view.case_id,run,'chat',.01)
            try:
                yield output()
                await asyncio.sleep(1)
            finally:
                await asyncio.sleep(.01)
                journal.settle(attempt,cost_usd=.001,outcome='ok')
                self.drained=True
    port=Port();observation=asyncio.run(run_case(request,1,manifest,journal,port))
    assert port.drained and observation.status=='timeout'
    assert observation.attempts[0].cost_usd==.001 and len(observation.outputs)==1

def test_repeated_cancellation_drains_and_persists_before_propagation(state):
    _,manifest,journal,request=state
    entered=asyncio.Event();cleanup=asyncio.Event()
    class Port:
        async def execute(self,view,run):
            attempt=journal.reserve(view.case_id,run,'chat',.01)
            try:
                entered.set();yield output();await asyncio.sleep(10)
            finally:
                cleanup.set();await asyncio.sleep(.01)
                journal.settle(attempt,cost_usd=.002,outcome='error')
    async def scenario():
        task=asyncio.create_task(run_case(request,1,manifest,journal,Port()))
        await entered.wait();task.cancel();await cleanup.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(scenario());row=journal.get(request.case_id,1)
    assert row.status=='error' and row.attempts[0].cost_usd==.002 and len(row.outputs)==1

def test_prior_orphan_attempt_never_reexecutes(state):
    _,manifest,journal,request=state
    journal.reserve(request.case_id,1,'chat',.01)
    class Port:
        async def execute(self,*args):raise AssertionError('must not execute');yield output()
    observation=asyncio.run(run_case(request,1,manifest,journal,Port()))
    assert observation.status=='error' and observation.attempts[0].cost_usd is None

def test_claim_without_result_is_not_automatically_replayed(state):
    _,manifest,journal,request=state
    import hashlib
    journal.claim_case(request.case_id,1,hashlib.sha256(request.model_dump_json().encode()).hexdigest())
    class Port:
        async def execute(self,*args):raise AssertionError('must not execute');yield output()
    with pytest.raises(EvaluationError,match='claimed_review_required'):asyncio.run(run_case(request,1,manifest,journal,Port()))

def test_dataset_all_cases_three_runs_serial_and_gold_free(state):
    pack,manifest,journal,_=state
    class Port:
        active=0;maximum=0
        async def execute(self,view,run):
            assert set(view.model_dump()).isdisjoint({'turns','required_evidence_units','forbidden_disclosure','expected'})
            self.active+=1;self.maximum=max(self.maximum,self.active)
            try:
                for _ in view.questions:yield output()
            finally:self.active-=1
    port=Port();results=asyncio.run(run_dataset(pack,manifest,journal,port,dataset_hash=manifest.dataset_sha256,rubric_hash=manifest.rubric_sha256))
    assert len(results)==72 and all(r.status=='ok' for r in results) and port.maximum==1

def test_provider_remains_disabled_before_any_claim_or_port(state):
    _,manifest,journal,request=state
    with pytest.raises(EvaluationError,match='provider_executor_not_installed'):
        asyncio.run(run_case(request,1,manifest.model_copy(update={'mode':'provider'}),journal,None))
    assert journal.get(request.case_id,1) is None

@pytest.mark.parametrize('run',[0,4,True,'1'])
def test_invalid_run_is_rejected_before_claim_or_execution(state,run):
    _,manifest,journal,request=state
    with pytest.raises(EvaluationError,match='invalid_evaluation_run'):
        asyncio.run(run_case(request,run,manifest,journal,None))
    assert journal.get(request.case_id,run) is None

def test_global_attempt_cap_is_durable_and_counts_preprocessing(state,tmp_path):
    _,base,_,request=state
    manifest=base.model_copy(update={'max_inference_attempts':1})
    journal=RunJournal(tmp_path/'capped.sqlite',manifest)
    attempt=journal.reserve('__preprocessing__',0,'document_embedding',.01)
    journal.settle(attempt,cost_usd=.001)
    journal=RunJournal(tmp_path/'capped.sqlite',manifest)
    with pytest.raises(EvaluationError,match='phase_attempt_limit'):journal.reserve(request.case_id,1,'chat',.01)

def test_phase_deadline_stops_before_next_case_and_preserves_current(state,tmp_path):
    pack,base,_,_=state;manifest=base.model_copy(update={'phase_timeout_ms':5})
    journal=RunJournal(tmp_path/'phase.sqlite',manifest)
    class Port:
        started=[]
        async def execute(self,request,run):
            self.started.append((request.case_id,run))
            yield output();await asyncio.sleep(1)
    port=Port()
    with pytest.raises(TimeoutError):
        asyncio.run(run_dataset(pack,manifest,journal,port,dataset_hash=manifest.dataset_sha256,rubric_hash=manifest.rubric_sha256))
    assert len(port.started)==1
    case_id,run=port.started[0]
    assert journal.get(case_id,run).status=='error'

def test_budget_stop_prevents_starting_another_case(state):
    pack,manifest,journal,_=state
    class Port:
        started=0
        async def execute(self,view,run):
            self.started+=1;journal.request_stop('budget_reservation_rejected')
            for _ in view.questions:yield output()
    port=Port()
    with pytest.raises(EvaluationError,match='phase_budget_stopped'):
        asyncio.run(run_dataset(pack,manifest,journal,port,dataset_hash=manifest.dataset_sha256,rubric_hash=manifest.rubric_sha256))
    assert port.started==1

def test_published_service_unavailable_is_error_not_success(state):
    _,manifest,journal,request=state
    class Port:
        async def execute(self,view,run):
            for _ in view.questions:yield output().model_copy(update={'kind':'service_unavailable'})
    assert asyncio.run(run_case(request,1,manifest,journal,Port())).status=='error'
