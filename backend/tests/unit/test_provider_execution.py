import asyncio,time,json,copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from enterprise_ai_platform.evaluation.rag import Manifest,load_pack,strict_json,configuration_hash,pack_content_hash,preflight,Output,EvaluationError,report
from enterprise_ai_platform.evaluation.provider import issue_provider_execution,permitted
from enterprise_ai_platform.evaluation.journal import RunJournal
from enterprise_ai_platform.evaluation.execution import ExecutionRequest
from enterprise_ai_platform.evaluation.runner import run_case,finalize_interrupted_case
from scripts.evaluation.provider_runtime import ProviderModels,run_lock
from enterprise_ai_platform.modules.models.domain import Message,ModelFailure
ROOT=Path(__file__).resolve().parents[3]
RATES=dict(chat_input=1.08,chat_output=4.32,embedding_input=.1,embedding_output=0.)

def state(tmp_path):
    pack=load_pack(ROOT/'evaluation/rag/v1/pack.json');base=Manifest.model_validate(strict_json((ROOT/'evaluation/rag/v1/fixture-config.json').read_text()))
    config=base.model_copy(update=dict(mode='provider',split='calibration',dataset_content_sha256=pack_content_hash(pack),model_id='apac.amazon.nova-pro-v1:0',embedding_model_id='cohere.embed-multilingual-v3',document_embedding_model_id='cohere.embed-multilingual-v3',region='ap-southeast-1',data_scope='fictional-only APAC exception',representative_embeddings=True,approved_by='fixture gold',budget_approved_by='fixture budget',provider_egress_approved=True,budget_limit_usd=1.,max_inference_attempts=148,case_timeout_ms=120000,phase_timeout_ms=900000,frozen_at='fixture freeze',price_snapshot_ref='fixture price'))
    permit=issue(config);journal=RunJournal(tmp_path/'corpus.sqlite',config);return pack,config,permit,journal

def issue(config,**extra):return issue_provider_execution(config,scope_sha256='a'*64,gold_dataset_sha256=config.dataset_sha256,expires_at=time.time()+800,verify_sources=lambda:None,**extra)

@pytest.mark.parametrize('change',[dict(mode='fixture'),dict(split='acceptance'),dict(model_id='another'),dict(region='us-east-1'),dict(budget_limit_usd=2.),dict(max_inference_attempts=149),dict(provider_egress_approved=False),dict(representative_embeddings=False)])
def test_capability_rejects_model_region_scope_caps_and_unapproved_egress(tmp_path,change):
    _,config,_,_=state(tmp_path)
    with pytest.raises(EvaluationError):issue(config.model_copy(update=change))

def test_editable_approval_strings_or_global_flags_cannot_enable_provider(tmp_path):
    pack,c,permit,j=state(tmp_path);r=ExecutionRequest.from_case(next(x for x in pack.cases if x.split=='calibration'))
    port=SimpleNamespace(execute=None)
    for bad in (None,True,SimpleNamespace(allows=lambda *_:True)):
        assert not permitted(bad,c)
        with pytest.raises(EvaluationError,match='provider_executor'):asyncio.run(run_case(r,1,c,j,port,executor=bad))
    assert permitted(permit,c)

def test_expiry_or_source_tamper_blocks_before_sdk_and_retains_report_scope(tmp_path,monkeypatch):
    _,c,cap,j=state(tmp_path);sdk=Mock();models=ProviderModels(c,cap,sdk,RATES).build(j,'case',1,lambda *_:time.time()+50)
    now=time.time();monkeypatch.setattr('enterprise_ai_platform.evaluation.provider.time.time',lambda:now+1000)
    with pytest.raises(ModelFailure):models.chat.chat([Message('user',text='Fictional')])
    assert not sdk.converse.called and permitted(cap,c,egress=False) and not permitted(cap,c)

def handoff(tmp_path):
    pack,c,cap,old=state(tmp_path);cap.bind_journal(old);ident=old.reserve('__preprocessing__:doc',0,'document_embedding',.001);old.settle(ident,cost_usd=.0001,input_tokens=10,output_tokens=0,outcome='ok');attempts=old.attempts('__preprocessing__:doc',0)
    final=c.model_copy(update=dict(preprocessing_attempts=attempts,corpus_embedding_provenance_sha256='b'*64));new=RunJournal(tmp_path/'cases.sqlite',final);return c,cap,old,final,new

def test_handoff_retires_old_capability_and_durable_ledger_and_preserves_scope(tmp_path):
    c,cap,old,final,new=handoff(tmp_path);sdk=Mock();prior=ProviderModels(c,cap,sdk,RATES).build(old,'prior',1,lambda *_:time.time()+50)
    transferred=cap.handoff(c,final,old,new)
    assert not permitted(cap,c) and permitted(transferred,final) and transferred.expires_at==cap.expires_at
    assert old.stop_requested()=='corpus_transferred' and RunJournal(old.path,c).stop_requested()=='corpus_transferred'
    with pytest.raises(Exception):prior.chat.chat([Message('user',text='Fictional')])
    with pytest.raises(EvaluationError):cap.bind_journal(RunJournal(tmp_path/'fresh.sqlite',c))
    assert not sdk.converse.called

@pytest.mark.parametrize('corrupt',['after_manifest','copied_ledger','unknown_corpus','budget_change'])
def test_handoff_cannot_omit_change_spend_or_restart_budget(tmp_path,corrupt):
    c,cap,old,final,new=handoff(tmp_path)
    if corrupt=='after_manifest':final=final.model_copy(update={'preprocessing_attempts':[]});new=RunJournal(tmp_path/'empty.sqlite',final)
    if corrupt=='copied_ledger':
        with new.connect() as db:db.execute("UPDATE attempts SET actual='0.00005'")
    if corrupt=='unknown_corpus':old.reserve('__preprocessing__:other',0,'document_embedding',.001)
    if corrupt=='budget_change':final=final.model_copy(update={'budget_limit_usd':.9});new=RunJournal(tmp_path/'changed.sqlite',final)
    with pytest.raises(EvaluationError):cap.handoff(c,final,old,new)
    assert old.stop_requested() is None

def test_pending_corpus_provenance_has_no_case_permission_until_verified_handoff(tmp_path):
    pack,c,cap,j=state(tmp_path)
    assert 'corpus_embedding_provenance_sha256' in preflight(pack,c,c.dataset_sha256,c.rubric_sha256,executor=cap)
    assert 'human_gold_review' not in preflight(pack,c,c.dataset_sha256,c.rubric_sha256,executor=cap)
    mutated=pack.model_copy(deep=True);mutated.cases[0].turns[0].required_material_facts=['forged']
    with pytest.raises(EvaluationError,match='gold_changed'):report(mutated,c,[],[],dataset_hash=c.dataset_sha256,rubric_hash=c.rubric_sha256,executor=cap)

def test_explicit_crash_finalization_keeps_partial_and_unknown_spend_without_replay(tmp_path):
    pack,c,cap,j=state(tmp_path);request=ExecutionRequest.from_case(next(x for x in pack.cases if x.split=='calibration' and len(x.turns)==2))
    import hashlib
    j.claim_case(request.case_id,1,hashlib.sha256(request.model_dump_json().encode()).hexdigest());j.reserve(request.case_id,1,'chat',.01);j.save_partial(request.case_id,1,Output(kind='clarification',text='Which policy?',final_retrieved_units=[],cited_units=[]))
    o=finalize_interrupted_case(request,1,c,j,reason='process_interrupted')
    assert o.status=='error' and len(o.outputs)==1 and o.attempts[0].cost_usd is None and not o.accounting_complete
    assert finalize_interrupted_case(request,1,c,j,reason='process_interrupted')==o
    with pytest.raises(EvaluationError):finalize_interrupted_case(request.model_copy(update={'questions':['changed']}),1,c,j,reason='process_interrupted')

def test_operator_recovery_lock_rejects_active_worker(tmp_path):
    with run_lock(tmp_path):
        with pytest.raises(EvaluationError,match='active'):
            with run_lock(tmp_path):raise AssertionError('two owners')
    with run_lock(tmp_path):pass


def test_source_tamper_stops_whole_provider_phase_before_any_sdk(tmp_path):
    _,c,_,j=state(tmp_path);mutable={'changed':False}
    def verify():
        if mutable['changed']:raise EvaluationError('changed')
    cap=issue_provider_execution(c,scope_sha256='a'*64,gold_dataset_sha256=c.dataset_sha256,expires_at=time.time()+800,verify_sources=verify)
    sdk=Mock();models=ProviderModels(c,cap,sdk,RATES).build(j,'c',1,lambda *_:time.time()+50);mutable['changed']=True
    with pytest.raises(ModelFailure):models.chat.chat([Message('user',text='Fictional')])
    assert not sdk.converse.called and j.stop_requested()=='provider_scope_rejected'


@pytest.mark.parametrize('problem',['no_live','wrong_approval_hash','wrong_scope','already_claimed'])
def test_cli_requires_explicit_scoped_consent_before_readiness_or_runtime_clients(tmp_path,monkeypatch,problem):
    from scripts.evaluation import rag_provider as cli
    from enterprise_ai_platform.authorization_check import CheckFailure
    approval=dict(approved_by='William',scope_sha256='wrong' if problem=='wrong_scope' else 'a'*64,target_budget_usd=1,fictional_apac=True)
    (tmp_path/'budget-approval.json').write_text(json.dumps(approval));(tmp_path/'run-request.json').write_text('{}')
    fake_config=SimpleNamespace(budget_limit_usd=1.);monkeypatch.setattr(cli,'frozen',lambda _: (None,fake_config,{}));ready=Mock(side_effect=AssertionError('must deny before credentials'));monkeypatch.setattr(cli,'readiness',ready)
    real_digest=cli.digest
    monkeypatch.setattr(cli,'digest',lambda p:'a'*64 if p.name=='run-request.json' else real_digest(p))
    if problem=='already_claimed':(tmp_path/'once.claim').write_text('prior run')
    with pytest.raises(CheckFailure):cli.run(tmp_path,'wrong' if problem=='wrong_approval_hash' else real_digest(tmp_path/'budget-approval.json'),problem!='no_live')
    assert not ready.called


def test_real_provider_proof_never_reuses_fixture_zero_call_or_embedding_labels(tmp_path):
    from scripts.evaluation.rag_provider import provider_evidence
    (tmp_path/'egress.jsonl').write_text('{"kind":"chat"}\n{"kind":"embedding"}\n')
    result=provider_evidence(dict(passed=True,actual_paid_inference_calls=0,ingestion='controlled_storage_parser_embedding'),tmp_path)
    assert result['attempted_sdk_calls']==2 and result['execution_mode']=='provider'
    assert 'actual_paid_inference_calls' not in result and 'ingestion' not in result


def test_recorded_sdk_logs_only_sanitized_protocol_and_preserves_invalid_quote_response(tmp_path):
    from scripts.evaluation.rag_provider import RecordedSDK
    payload=dict(question='Private fictional question',sources=[dict(id='S1',text='Fictional limit12.'),dict(id='S2',text='Fictional limit17.')])
    body=json.dumps(dict(kind='answer',citations=[dict(id='S2',quote='Fictional limit12.')]))
    response=dict(stopReason='end_turn',output=dict(message=dict(content=[dict(text=body)])))
    client=SimpleNamespace(converse=lambda **_:response)
    sdk=RecordedSDK(client,tmp_path);received=sdk.converse(messages=[dict(content=[dict(text='Instructions\n'+json.dumps(payload))])])
    assert received is response
    notes=(tmp_path/'protocol-notes.jsonl').read_text();assert 'fictional' not in notes.lower() and 'Private' not in notes
    assert json.loads(notes)['citation_sentence_exact']==[False]


def test_revised_phase_allocation_requires_its_own_approval_hash_and_amount(tmp_path,monkeypatch):
    from scripts.evaluation import rag_provider as cli
    from enterprise_ai_platform.authorization_check import CheckFailure
    approval=dict(approved_by='William',scope_sha256='a'*64,target_budget_usd=1,fictional_apac=True)
    file=tmp_path/'budget-approval.json';file.write_text(json.dumps(approval));(tmp_path/'run-request.json').write_text('{}')
    monkeypatch.setattr(cli,'frozen',lambda _:(None,SimpleNamespace(budget_limit_usd=.9),{}));ready=Mock(side_effect=AssertionError('no new scope approval'));monkeypatch.setattr(cli,'readiness',ready);real=cli.digest;monkeypatch.setattr(cli,'digest',lambda p:'a'*64 if p.name=='run-request.json' else real(p))
    with pytest.raises(CheckFailure):cli.run(tmp_path,real(file),True)
    assert not ready.called
