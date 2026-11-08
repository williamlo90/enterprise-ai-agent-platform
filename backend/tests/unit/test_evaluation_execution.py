"""Actual Nova/Cohere adapters with controlled SDK; no provider clients."""
import importlib.util
import io
import json
import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ReadTimeoutError
from enterprise_ai_platform.evaluation.execution import ActorBinding,ExecutionRequest,SourceBinding,SourceCatalog
from enterprise_ai_platform.evaluation.journal import RunJournal
from enterprise_ai_platform.evaluation.rag import EvaluationError,Manifest,load_pack,strict_json
from enterprise_ai_platform.integrations.models.nova import NovaChat,CohereEmbedding
from enterprise_ai_platform.modules.models.domain import Message,ModelFailure,Usage
from enterprise_ai_platform.modules.rag.domain import context,render,sha

ROOT=Path(__file__).resolve().parents[3]
spec=importlib.util.spec_from_file_location('eval_models',ROOT/'scripts/evaluation/model_port.py')
bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
RATES={'chat_input':1.08,'chat_output':4.32,'embedding_input':.1,'embedding_output':0}

def setup(tmp_path,*,budget=1.0,authority=None,max_calls=8,clock=lambda:0):
    config=Manifest.model_validate(strict_json((ROOT/'evaluation/rag/v1/fixture-config.json').read_text(encoding='utf-8')))
    journal=RunJournal(tmp_path/'ledger.sqlite',config.model_copy(update={'budget_limit_usd':budget}))
    client=Mock()
    def factory(before):return SimpleNamespace(chat=NovaChat(client,before),embedding=CohereEmbedding(client,before))
    models=bridge.BudgetedModels(journal,'c',1,RATES,authority or (lambda *_:100),factory,max_calls=max_calls,clock=clock)
    client.converse.return_value={'output':{'message':{'role':'assistant','content':[{'text':'fictional'}]}},'stopReason':'end_turn','usage':{'inputTokens':20,'outputTokens':10,'totalTokens':30}}
    return models,journal,client

def chat(models):return models.chat.chat([Message('user',text='Fictional policy question')])

def test_request_has_no_gold_or_expected_fields():
    pack=load_pack(ROOT/'evaluation/rag/v1/pack.json')
    case=next(c for c in pack.cases if c.behavior=='clarification')
    view=ExecutionRequest.from_case(case)
    assert view.questions==[t.question for t in case.turns]
    assert set(view.model_dump())=={'case_id','tenant','principal_id','membership_id','access_revision','language','evaluated_at','questions'}
    assert not {'required_material_facts','required_evidence_units','forbidden_disclosure','expected'}&set(view.model_dump())

def test_actor_initialization_revision_is_frozen_and_later_drift_denied():
    pack=load_pack(ROOT/'evaluation/rag/v1/pack.json');request=ExecutionRequest.from_case(pack.cases[0])
    binding=ActorBinding(tenant=request.tenant,tenant_id='actual-tenant',principal_id=request.principal_id,membership_id=request.membership_id,authored_access_revision=1,runtime_access_revision=2)
    binding.verify(request,'actual-tenant',2)
    with pytest.raises(EvaluationError):binding.verify(request,'actual-tenant',3)
    with pytest.raises(EvaluationError):binding.verify(request,'another-tenant',2)
    with pytest.raises(EvaluationError):binding.verify(request.model_copy(update={'access_revision':2}),'actual-tenant',2)

def test_source_mapping_uses_actual_context_identity_and_digest():
    text='Fictional claims are due within seven days.'
    hit=dict(version_id='version1',version_no=1,page=1,chunk_no=0,index_generation=2,access_revision=3,text_digest=sha(text),text=text,start=0)
    binding=SourceBinding(unit_id='unit1',**{k:v for k,v in hit.items() if k not in ('text','start')})
    catalog=SourceCatalog([binding]);ctx=context('What is the deadline?','en',[hit])
    output=render({'kind':'answer','citations':[{'id':'S1','quote':text}]},ctx,'en')
    measured=catalog.measure(ctx,output)
    assert measured.final_retrieved_units==measured.cited_units==['unit1']
    for field,value in [('access_revision',4),('text_digest','a'*64),('index_generation',3),('text','tampered'),('version_id','foreign')]:
        changed=SimpleNamespace(hits=({**hit,field:value},))
        with pytest.raises(EvaluationError):catalog.measure(changed,output)
    output['citations'][0]['version_id']='foreign'
    with pytest.raises(EvaluationError):catalog.measure(ctx,output)

def test_reservation_and_authority_both_precede_actual_sdk(tmp_path):
    calls=[]
    models,journal,client=setup(tmp_path,authority=lambda *args:(calls.append(args),100)[1])
    def response(**kwargs):
        row=journal.attempts('c',1)
        assert len(row)==1 and row[0].cost_usd is None and calls==[('chat',4096)]
        return client.converse.return_value
    client.converse.side_effect=response
    chat(models)
    attempt=journal.attempts('c',1)[0]
    assert attempt.outcome=='ok' and attempt.cost_usd==pytest.approx(.0000648)
    assert models.chat.last_usage==Usage(20,10)

def test_budget_denial_cannot_reach_sdk(tmp_path):
    models,journal,client=setup(tmp_path,budget=.0001)
    with pytest.raises(ModelFailure):chat(models)
    client.converse.assert_not_called();assert journal.attempts('c',1)==[]

def test_authority_denial_cannot_reach_sdk_or_reserve(tmp_path):
    def deny(*args):raise ValueError('controlled denial')
    models,journal,client=setup(tmp_path,authority=deny)
    with pytest.raises(ModelFailure):chat(models)
    client.converse.assert_not_called();assert journal.attempts('c',1)==[]

def test_expiry_after_durable_reservation_records_zero_no_egress(tmp_path):
    ticks=iter([0,101]);models,journal,client=setup(tmp_path,clock=lambda:next(ticks))
    with pytest.raises(ModelFailure):chat(models)
    client.converse.assert_not_called()
    assert journal.attempts('c',1)[0].cost_usd==0

def test_unknown_timeout_retains_exposure_and_usage_resets(tmp_path):
    models,journal,client=setup(tmp_path)
    chat(models)
    client.converse.side_effect=ReadTimeoutError(endpoint_url='https://controlled.example.test')
    with pytest.raises(ModelFailure,match='timeout'):chat(models)
    rows=journal.attempts('c',1)
    assert len(rows)==2 and rows[-1].outcome=='timeout' and rows[-1].cost_usd is None
    assert rows[-1].unknown_reserved_estimate_usd>0 and models.chat.last_usage is None

def test_parse_error_preserves_known_provider_usage(tmp_path):
    models,journal,client=setup(tmp_path)
    client.converse.return_value['stopReason']='max_tokens'
    with pytest.raises(ModelFailure):chat(models)
    row=journal.attempts('c',1)[0]
    assert row.outcome=='error' and row.cost_usd==pytest.approx(.0000648)

def test_settlement_failure_does_not_hide_usage_from_outer_application(tmp_path):
    models,journal,client=setup(tmp_path)
    models.guard.settle=Mock(side_effect=OSError('controlled ledger failure'))
    with pytest.raises(OSError):chat(models)
    assert models.chat.last_usage==Usage(20,10) and client.converse.call_count==1
    assert journal.attempts('c',1)[0].cost_usd is None

def test_reservation_io_failure_stops_phase_before_sdk(tmp_path):
    models,journal,client=setup(tmp_path)
    models.guard.reserve=Mock(side_effect=OSError('controlled write failure'))
    with pytest.raises(ModelFailure):chat(models)
    assert journal.stop_requested()=='ledger_failure'
    client.converse.assert_not_called()

def test_known_overrun_preserves_cost_and_blocks_next_sdk(tmp_path):
    models,journal,client=setup(tmp_path)
    # Usage can arrive even after an adapter rejects a token-bound violation.
    client.converse.return_value['usage']={'inputTokens':6000,'outputTokens':10,'totalTokens':6010}
    with pytest.raises(ModelFailure):chat(models)
    row=journal.attempts('c',1)[0]
    assert row.input_tokens==6000 and row.cost_usd>0
    with pytest.raises(ModelFailure):chat(models)
    assert client.converse.call_count==1

@pytest.mark.parametrize('input_type,expected',[('search_query','query_embedding'),('search_document','document_embedding')])
def test_embedding_purpose_is_distinct_and_known_usage_settles(tmp_path,input_type,expected):
    observed=[];models,journal,client=setup(tmp_path,authority=lambda *args:(observed.append(args),100)[1])
    client.invoke_model.return_value={'body':io.BytesIO(json.dumps({'embeddings':[[1.0]+[0.0]*1023]}).encode()),'ResponseMetadata':{'HTTPHeaders':{'x-amzn-bedrock-input-token-count':'12'}}}
    models.embedding.embed(['Fictional source'],input_type=input_type)
    row=journal.attempts('c',1)[0]
    assert row.kind==expected and row.cost_usd==pytest.approx(.0000012)
    assert observed==[('embedding',512)]
    assert json.loads(client.invoke_model.call_args.kwargs['body'])['input_type']==input_type

def test_call_cap_survives_reopened_journal(tmp_path):
    models,journal,client=setup(tmp_path,max_calls=1)
    chat(models)
    with pytest.raises(ModelFailure):chat(models)
    assert client.converse.call_count==1 and len(journal.attempts('c',1))==1

def test_invalid_input_does_not_reuse_prior_usage_or_reserve(tmp_path):
    models,journal,client=setup(tmp_path);chat(models)
    with pytest.raises(ModelFailure):models.chat.chat([Message('user',text='x'*4000)])
    assert client.converse.call_count==1 and len(journal.attempts('c',1))==1
    assert models.chat.last_usage is None

def test_invalid_embedding_purpose_clears_prior_usage_without_sdk(tmp_path):
    models,journal,client=setup(tmp_path)
    models.embedding.last_usage=Usage(12,0)
    with pytest.raises(EvaluationError):models.embedding.embed(['source'],input_type='invalid')
    assert models.embedding.last_usage is None and journal.attempts('c',1)==[]
    client.invoke_model.assert_not_called()

def test_corpus_phase_repeated_cancel_drains_actual_embedding_sdk_thread(tmp_path):
    import sys
    sys.path.insert(0,str(ROOT/'scripts/evaluation'))
    from dataset_runtime import DatasetHarness
    from enterprise_ai_platform.rag_check import Harness as RagHarness
    assert DatasetHarness.execute is RagHarness.execute
    models,journal,client=setup(tmp_path)
    entered=threading.Event();release=threading.Event();drained=asyncio.Event()
    def slow_sdk(**kwargs):
        entered.set()
        if not release.wait(5):raise RuntimeError('controlled test release timeout')
        return {'body':io.BytesIO(json.dumps({'embeddings':[[1.0]+[0.0]*1023]}).encode()),'ResponseMetadata':{'HTTPHeaders':{'x-amzn-bedrock-input-token-count':'12'}}}
    client.invoke_model.side_effect=slow_sdk
    harness=object.__new__(DatasetHarness);harness.phase_deadline=time.monotonic()+10
    async def ingestion():
        # Same single-shield cleanup shape used by KnowledgeIngestion.process.
        pending=asyncio.create_task(asyncio.to_thread(models.embedding.embed,['fictional'],input_type='search_document'))
        try:await asyncio.shield(pending)
        except asyncio.CancelledError:
            drained.set();await asyncio.shield(pending);raise
    harness.ingest_corpus=ingestion
    async def scenario():
        task=asyncio.create_task(harness.prepare_corpus())
        while not entered.is_set():await asyncio.sleep(.001)
        task.cancel();await drained.wait();task.cancel();await asyncio.sleep(.001)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
    try:asyncio.run(scenario())
    finally:release.set()
    row=journal.attempts('c',1)[0]
    assert row.cost_usd==pytest.approx(.0000012) and client.invoke_model.call_count==1
