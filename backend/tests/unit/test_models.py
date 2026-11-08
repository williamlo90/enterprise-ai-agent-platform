import io
import json
from decimal import Decimal
from pathlib import Path
from botocore.config import Config
import boto3
from botocore.response import StreamingBody
from botocore.stub import Stubber
from botocore.exceptions import ReadTimeoutError,ClientError
import pytest
from enterprise_ai_platform.integrations.models.bedrock import BedrockModels,SDK_CONFIG,ENDPOINT,strict_json,MAX_RESPONSE_BYTES
from enterprise_ai_platform.modules.models.domain import CHAT_MODEL,EMBED_MODEL,REGION,DIMENSIONS,ModelFailure,Usage,vectors,tool_call
from enterprise_ai_platform import model_check as probe


def response():
    return {'output':{'message':{'role':'assistant','content':[{'text':'7 days.'}]}},'stopReason':'end_turn',
            'usage':{'inputTokens':12,'outputTokens':4,'totalTokens':16},'ResponseMetadata':{'HTTPStatusCode':200,'RetryAttempts':0}}


class Client:
    def __init__(self,value=None):self.value=response() if value is None else value;self.calls=[];self.fail=None
    def count_tokens(self,**kwargs):self.calls.append(('count_tokens',kwargs));return {'inputTokens':12}
    def converse(self,**kwargs):
        self.calls.append(('converse',kwargs))
        if self.fail:raise self.fail
        return self.value
    def invoke_model(self,**kwargs):
        self.calls.append(('invoke_model',kwargs))
        if self.fail:raise self.fail
        return self.value


def chat(adapter,tools=False):return adapter.chat([{'role':'user','content':[{'text':'fictional policy'}]}],tools=tools)
def adapter(client):return BedrockModels(client,lambda *args:None)
def tool(language='en',id='call_1'):return {'toolUseId':id,'name':'lookup_demo_policy','input':{'topic':'travel','language':language}}


def test_chat_reserves_before_paid_call_without_unsupported_count_tokens():
    c=Client();reservations=[];a=BedrockModels(c,lambda *args:reservations.append(args));r=chat(a)
    assert r.text=='7 days.' and r.usage==Usage(12,4) and '7 days' not in repr(r)
    assert [x[0] for x in c.calls]==['converse']
    request=c.calls[-1][1];assert request['modelId']==CHAT_MODEL and request['inferenceConfig']=={'maxTokens':256}
    assert request['additionalModelRequestFields']=={'thinking':{'type':'disabled'}} and reservations==[('chat',4096)]


@pytest.mark.parametrize('change',[
    {'stopReason':'max_tokens'},{'stopReason':'guardrail_intervened'},{'stopReason':'unknown'},
    {'usage':{'inputTokens':True,'outputTokens':4,'totalTokens':5}},
    {'usage':{'inputTokens':12,'outputTokens':4,'totalTokens':18}},
    {'usage':{'inputTokens':12,'outputTokens':257,'totalTokens':269}},
    {'usage':{'inputTokens':12,'outputTokens':4,'totalTokens':16,'cacheWriteInputTokens':1}},
    {'output':{'message':{'role':'user','content':[{'text':'private'}]}}},
    {'output':{'message':{'role':'assistant','content':[{'text':'private','toolUse':tool()}]}}},
    {'output':{'message':{'role':'assistant','content':[{'text':''}]}}},
    {'output':{'message':{'role':'assistant','content':[{'text':'x'*4097}]}}},
])
def test_malformed_chat_and_incomplete_stop_rejected(change):
    r=response();r.update(change)
    with pytest.raises(ModelFailure):chat(adapter(Client(r)),tools=True)


@pytest.mark.parametrize('value',[{},None,{'toolUseId':'bad/id','name':'lookup_demo_policy','input':{}},
    {'toolUseId':'id','name':'delete_all','input':{}},
    {'toolUseId':'id','name':'lookup_demo_policy','input':{'topic':'travel','language':'en','tenant':'other'}},
    {'toolUseId':'id','name':'lookup_demo_policy','input':{'topic':'travel','language':'fr'}}])
def test_only_exact_demo_tool_and_arguments_allowed(value):
    with pytest.raises(ModelFailure):tool_call(value)


def test_tool_call_returned_as_data_and_duplicate_calls_rejected():
    r=response();r['output']['message']['content']=[{'toolUse':tool()}];r['stopReason']='tool_use'
    out=chat(adapter(Client(r)),tools=True);assert out.tool.language=='en' and out.tool.id=='call_1'
    with pytest.raises(ModelFailure):chat(adapter(Client(r)),tools=False)
    r['output']['message']['content'].append({'toolUse':tool()})
    with pytest.raises(ModelFailure):chat(adapter(Client(r)),tools=True)


def embedding_response(value=None):
    data={'embeddings':[[1.0]+[0.0]*(DIMENSIONS-1)],'texts':['fictional']} if value is None else value
    raw=json.dumps(data).encode()
    return {'body':StreamingBody(io.BytesIO(raw),len(raw)),'ResponseMetadata':{'HTTPStatusCode':200,'RetryAttempts':0}}


def test_embedding_fixed_dimension_no_silent_truncation_and_no_invented_usage():
    c=Client(embedding_response());r=adapter(c).embed(['fictional'],input_type='search_document')
    assert len(r.vectors[0])==1024 and r.usage is None and '1.0' not in repr(r)
    request=c.calls[-1][1];body=json.loads(request['body'])
    assert request['modelId']==EMBED_MODEL and body=={'texts':['fictional'],'input_type':'search_document','truncate':'NONE','embedding_types':['float']}


@pytest.mark.parametrize('value',[
    {'embeddings':[[1.0]*1023]}, {'embeddings':[[1.0]*1025]}, {'embeddings':[[0.0]*1024]},
    {'embeddings':[[True]+[1.0]*1023]}, {'embeddings':[[float('nan')]+[1.0]*1023]},
    {'embeddings':[[float('inf')]+[1.0]*1023]}, {'embeddings':[[1.0]*1024]*2},
    {'embeddings':{'int8':[[1]*1024]}}, {'embeddings':[[1.0]*1024],'texts':['reordered']},
])
def test_embedding_invalid_shape_values_count_or_echo_fails(value):
    with pytest.raises(ModelFailure):adapter(Client(embedding_response(value))).embed(['fictional'],input_type='search_query')


def test_huge_integer_cannot_escape_vector_validation_as_overflow():
    with pytest.raises(ModelFailure):vectors([[10**1000]+[1.0]*1023],1)


@pytest.mark.parametrize('raw',[b'{"a":1,"a":2}',b'{"a":NaN}',b'{"a":Infinity}',b'invalid'])
def test_json_ambiguity_and_nonfinite_values_rejected(raw):
    with pytest.raises(ModelFailure):strict_json(raw)


def test_embedding_oversize_stream_closed_and_read_bound():
    class Stream:
        closed=False
        def read(self,n):assert n==MAX_RESPONSE_BYTES+1;return b'x'*n
        def close(self):self.closed=True
    stream=Stream()
    with pytest.raises(ModelFailure):adapter(Client({'body':stream})).embed(['fictional'],input_type='search_query')
    assert stream.closed


@pytest.mark.parametrize('code,expected',[('ThrottlingException','rate_limited'),('AccessDeniedException','access_denied'),('InternalServerException','provider_unavailable'),('ValidationException','invalid_input'),('ModelTimeoutException','timeout')])
def test_provider_failures_are_sanitized_and_not_retried(code,expected):
    c=Client();c.fail=ClientError({'Error':{'Code':code,'Message':'private prompt credentials'}},'Converse')
    with pytest.raises(ModelFailure) as e:chat(adapter(c))
    assert e.value.code==expected and 'private' not in str(e.value) and len(c.calls)==1


def test_timeout_does_not_retry_or_call_another_model():
    c=Client();c.fail=ReadTimeoutError(endpoint_url=ENDPOINT)
    with pytest.raises(ModelFailure,match='timeout'):chat(adapter(c))
    assert sum(op=='converse' for op,_ in c.calls)==1


def proof(tmp_path):
    p=tmp_path/'proof.json';d={'attempts':[],'checks':[]};p.write_text(json.dumps(d));return probe.Proof(p,d)


def test_paid_call_requires_durable_reservation_before_provider(tmp_path,monkeypatch):
    p=proof(tmp_path);c=Client();a=BedrockModels(c,p.reserve)
    def fail():raise OSError('disk unavailable')
    monkeypatch.setattr(p,'save',fail)
    with pytest.raises(ModelFailure):chat(a)
    assert c.calls==[]


def test_failed_attempt_keeps_unknown_cost_and_reservation(tmp_path):
    p=proof(tmp_path);c=Client();c.fail=ReadTimeoutError(endpoint_url=ENDPOINT);a=BedrockModels(c,p.reserve)
    with pytest.raises(ModelFailure):p.perform(a,lambda:chat(a))
    row=json.loads(p.path.read_text())['attempts'][0]
    assert row['state']=='failed_usage_unknown' and row['actual_token_cost_usd'] is None and Decimal(row['reserved_usd'])>0


def test_failure_after_valid_usage_still_counts_known_token_cost(tmp_path):
    p=proof(tmp_path);r=response();r['stopReason']='max_tokens';a=BedrockModels(Client(r),p.reserve)
    with pytest.raises(ModelFailure):p.perform(a,lambda:chat(a))
    assert p.data['attempts'][0]['state']=='failed' and p.data['attempts'][0]['usage']['input_tokens']==12


def test_maximum_paid_attempts_and_whole_probe_deadline(tmp_path):
    p=proof(tmp_path)
    for _ in range(6):p.reserve('chat',4096);p.settle(None,False)
    for _ in range(2):p.reserve('embedding',2048);p.settle(None,False)
    assert sum(Decimal(r['reserved_usd']) for r in p.data['attempts'])<Decimal('0.10')
    with pytest.raises(ModelFailure,match='budget_exhausted'):p.reserve('chat',1)
    p=proof(tmp_path);p.start-=121
    with pytest.raises(ModelFailure,match='deadline'):p.reserve('chat',1)


def test_real_sdk_stubber_contract_for_sonnet_converse_without_counter():
    c=boto3.client('bedrock-runtime',region_name=REGION,endpoint_url=ENDPOINT,aws_access_key_id='fictional',aws_secret_access_key='fictional',config=SDK_CONFIG)
    with Stubber(c) as stub:
        r=response();r['metrics']={'latencyMs':1};stub.add_response('converse',r)
        assert chat(adapter(c)).usage==Usage(12,4)
        stub.assert_no_pending_responses()
    assert c.meta.config.retries['total_max_attempts']==1 and c.meta.region_name==REGION
    c.close()


def test_no_singapore_cross_region_profile_or_mutation_operations_in_cli():
    # Provider bindings are executable constants; setup mutation is user-only.
    assert CHAT_MODEL=='anthropic.claude-sonnet-5' and EMBED_MODEL=='cohere.embed-multilingual-v3'
    assert REGION=='ap-southeast-1' and ENDPOINT=='https://bedrock-runtime.ap-southeast-1.amazonaws.com'


@pytest.mark.parametrize('failure',['throttle','timeout'])
def test_actual_sdk_against_local_http_fault_server_never_retries(failure):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from threading import Thread
    import time
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            requests.append(self.path)
            if self.path.endswith('/count-tokens'):
                code=200;body=b'{"inputTokens":12}'
            else:
                if failure=='timeout':time.sleep(.2)
                code=429;body=b'{"__type":"ThrottlingException","message":"fictional fault"}'
            try:
                self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    client=boto3.client('bedrock-runtime',region_name=REGION,endpoint_url='http://127.0.0.1:'+str(server.server_port),
        aws_access_key_id='fictional',aws_secret_access_key='fictional',config=SDK_CONFIG.merge(Config(read_timeout=.05)))
    try:
        with pytest.raises(ModelFailure) as e:chat(adapter(client))
        assert e.value.code==('rate_limited' if failure=='throttle' else 'timeout')
        assert len(requests)==1 and requests[0].endswith('/converse')
    finally:client.close();server.shutdown();server.server_close();thread.join(timeout=2)


def test_hard_wall_marks_remote_outcome_unknown_and_preserves_evidence(tmp_path,monkeypatch):
    monkeypatch.chdir(Path(__file__).resolve().parents[3])
    folder=tmp_path/'spike02';folder.mkdir();(folder/'setup.json').write_text('{}');monkeypatch.setattr(probe,'FOLDER',folder)
    monkeypatch.setattr(probe,'preflight',lambda:({},dict(ready=True)))
    def timed_out(command,**kwargs):
        path=Path(command[-1]);data=json.loads(path.read_text())
        data['attempts'].append({'kind':'chat','state':'pending','usage':None,'actual_token_cost_usd':None,'reserved_usd':'0.01'})
        path.write_text(json.dumps(data))
        raise probe.subprocess.TimeoutExpired(command,150)
    monkeypatch.setattr(probe.subprocess,'run',timed_out)
    with pytest.raises(SystemExit):probe.run()
    path=next((folder/'runs').glob('*/proof.json'));before=path.read_bytes();data=json.loads(before)
    assert data['passed'] is False and data['remote_request_may_continue'] is True
    assert data['attempts'][0]['state']=='unknown_remote_outcome' and data['attempts'][0]['actual_token_cost_usd'] is None
    with pytest.raises(ModelFailure):probe.run()
    assert path.read_bytes()==before


class Control:
    agreement='AVAILABLE';mode='none';logging=False;form=True;price_drift=False
    def get_foundation_model(self,modelIdentifier):
        return {'modelDetails':{'modelId':modelIdentifier,'modelLifecycle':{'status':'ACTIVE'},'inferenceTypesSupported':['ON_DEMAND']}}
    def get_foundation_model_availability(self,**kwargs):
        return dict(authorizationStatus='AUTHORIZED',entitlementAvailability='AVAILABLE',regionAvailability='AVAILABLE',agreementAvailability={'status':self.agreement})
    def list_foundation_model_agreement_offers(self,modelId,**kwargs):
        rates=[('APS1_input_tokens_standard','2.2'),('APS1_output_tokens_standard','11')] if modelId==CHAT_MODEL else [('APS1_InputTokenCount','0.1')]
        return {'offers':[{'termDetails':{'usageBasedPricingTerm':{'rateCard':[{'dimension':k,'price':'100' if self.price_drift else v} for k,v in rates]}}}]}
    def get_account_data_retention(self):return {'mode':self.mode}
    def get_model_invocation_logging_configuration(self):return {'loggingConfig':{'textDataDeliveryEnabled':True}} if self.logging else {}
    def get_use_case_for_model_access(self):return {'formData':b'fictional' if self.form else b''}


def configured_preflight(tmp_path,monkeypatch):
    monkeypatch.setattr(probe,'FOLDER',tmp_path)
    setup=dict(region=REGION,chat_model=CHAT_MODEL,embedding_model=EMBED_MODEL,dimensions=1024,prices=probe.PRICES,
               budget_target_usd='1',probe_reservation_limit_usd='0.10',max_paid_attempts=8,account='fictional-account')
    (tmp_path/'setup.json').write_text(json.dumps(setup))
    class Identity:
        def get_caller_identity(self):return {'Account':'fictional-account'}
    control=Control();monkeypatch.setattr(probe,'clients',lambda:(control,Identity()))
    return control


@pytest.mark.parametrize('field,value,failed_check',[
    ('agreement','NOT_AVAILABLE','chat'),('mode','inherit','retention_none'),('mode','aws_review','retention_none'),
    ('logging',True,'raw_logging_disabled'),('form',False,'anthropic_form_present'),
])
def test_preflight_explains_blocking_access_or_privacy_gate(tmp_path,monkeypatch,field,value,failed_check):
    control=configured_preflight(tmp_path,monkeypatch);setattr(control,field,value)
    _,checks=probe.preflight()
    assert checks[failed_check] is False and not all(checks.values())


def test_preflight_price_change_blocks_before_inference(tmp_path,monkeypatch):
    control=configured_preflight(tmp_path,monkeypatch);control.price_drift=True
    with pytest.raises(ModelFailure):probe.preflight()


def test_preflight_current_account_region_models_and_privacy_pass(tmp_path,monkeypatch):
    configured_preflight(tmp_path,monkeypatch)
    _,checks=probe.preflight();assert all(checks.values())
    setup=json.loads((tmp_path/'setup.json').read_text());setup['region']='us-east-1';(tmp_path/'setup.json').write_text(json.dumps(setup))
    with pytest.raises(ModelFailure):probe.preflight()


@pytest.mark.parametrize('failure',['bad_json','read_timeout','invalid_vectors'])
def test_known_embedding_usage_survives_body_failures(tmp_path,failure):
    p=proof(tmp_path);value=embedding_response({'embeddings':[]})
    value['ResponseMetadata']['HTTPHeaders']={'x-amzn-bedrock-input-token-count':'12'}
    class Body:
        closed=False
        def read(self,n):
            if failure=='read_timeout':raise ReadTimeoutError(endpoint_url=ENDPOINT)
            return b'not-json' if failure=='bad_json' else b'{"embeddings":[]}'
        def close(self):self.closed=True
    body=Body();value['body']=body;a=BedrockModels(Client(value),p.reserve)
    with pytest.raises(ModelFailure):p.perform(a,lambda:a.embed(['fictional'],input_type='search_query'))
    row=p.data['attempts'][0]
    assert row['state']=='failed' and row['usage']=={'input_tokens':12,'output_tokens':0}
    assert Decimal(row['actual_token_cost_usd'])==Decimal('0.0000012') and body.closed


def test_concurrent_run_claim_allows_only_one_child(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from types import SimpleNamespace
    monkeypatch.chdir(Path(__file__).resolve().parents[3])
    folder=tmp_path/'spike02';folder.mkdir();(folder/'setup.json').write_text('{}');monkeypatch.setattr(probe,'FOLDER',folder)
    monkeypatch.setattr(probe,'preflight',lambda:({},dict(ready=True)))
    children=[];barrier=Barrier(2)
    def spawned(command,**kwargs):
        children.append(command);path=Path(command[-1]);data=json.loads(path.read_text());data.update(passed=True,status='passed');path.write_text(json.dumps(data))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(probe.subprocess,'run',spawned)
    def start():
        barrier.wait(timeout=3)
        try:probe.run();return True
        except ModelFailure:return False
    with ThreadPoolExecutor(max_workers=2) as pool:outcomes=list(pool.map(lambda _:start(),range(2)))
    assert sorted(outcomes)==[False,True] and len(children)==1
    assert len(list((folder/'runs').glob('*/proof.json')))==1


def test_duplicate_workers_on_one_proof_never_repeat_exercise(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    monkeypatch.chdir(Path(__file__).resolve().parents[3])
    folder=tmp_path/'spike02';directory=folder/'runs'/'fictional-run';directory.mkdir(parents=True)
    monkeypatch.setattr(probe,'FOLDER',folder);(folder/'run-claim.json').write_text(json.dumps({'run_id':'fictional-run'}))
    path=directory/'proof.json';path.write_text(json.dumps(dict(source_sha256=probe.fingerprint(),fixture_sha256=probe.fixture_hash(),status='running',attempts=[],checks=[])))
    monkeypatch.setattr(probe,'preflight',lambda:({},dict(ready=True)))
    class Runtime:
        def close(self):pass
    monkeypatch.setattr(probe.boto3,'client',lambda *args,**kwargs:Runtime())
    calls=[];monkeypatch.setattr(probe,'exercise',lambda *args:calls.append(True));barrier=Barrier(2)
    def start():
        barrier.wait(timeout=3)
        try:probe.worker(path);return True
        except ModelFailure:return False
    with ThreadPoolExecutor(max_workers=2) as pool:outcomes=list(pool.map(lambda _:start(),range(2)))
    assert sorted(outcomes)==[False,True] and calls==[True]


@pytest.mark.parametrize('mode,apply,writes',[('inherit',False,0),('inherit',True,1),('none',True,0)])
def test_operator_retention_helper_never_changes_without_apply_and_exact_scope(mode,apply,writes):
    import runpy
    configure=runpy.run_path(str(Path(__file__).resolve().parents[3]/'scripts/spikes/models/set_retention_none.py'))['configure']
    class Identity:
        def get_caller_identity(self):return {'Account':'fictional-account'}
    class Retention:
        def __init__(self):self.mode=mode;self.writes=[]
        def get_account_data_retention(self):return {'mode':self.mode}
        def put_account_data_retention(self,**kwargs):self.writes.append(kwargs);self.mode=kwargs['mode'];return {'mode':self.mode}
    control=Retention();setup={'region':REGION,'account':'fictional-account'}
    result=configure(setup,control,Identity(),apply=apply)
    assert len(control.writes)==writes and result['applied']==bool(writes)
    assert all(v=={'mode':'none'} for v in control.writes)
    with pytest.raises(ValueError):configure({'region':'us-east-1','account':'fictional-account'},control,Identity(),apply=True)
    with pytest.raises(ValueError):configure({'region':REGION,'account':'wrong'},control,Identity(),apply=True)
    control.mode='aws_review'
    with pytest.raises(ValueError):configure(setup,control,Identity(),apply=True)
    assert len(control.writes)==writes


def test_full_request_byte_limit_includes_system_and_tool_schema_before_paid_call():
    c=Client();a=adapter(c)
    with pytest.raises(ModelFailure,match='invalid_input'):
        a.chat([{'role':'user','content':[{'text':'a'*3000}]}],tools=True)
    assert c.calls==[]


def test_error_proof_records_safe_operation_and_code_not_provider_message(tmp_path):
    p=proof(tmp_path);c=Client();c.fail=ClientError({'Error':{'Code':'ValidationException','Message':'private-body-secret'}},'Converse')
    a=BedrockModels(c,p.reserve)
    with pytest.raises(ModelFailure):p.perform(a,lambda:chat(a))
    failure=p.data['attempts'][0]['failure']
    assert failure=={'code':'invalid_input','operation':'Converse','provider_code':'ValidationException'}
    assert 'private-body-secret' not in p.path.read_text()


def retry_fixture(tmp_path,monkeypatch):
    from uuid import uuid4
    import hashlib
    monkeypatch.chdir(Path(__file__).resolve().parents[3]);folder=tmp_path/'spike02'
    old_id=uuid4().hex;new_id=uuid4().hex;directory=folder/'runs'/old_id;directory.mkdir(parents=True)
    monkeypatch.setattr(probe,'FOLDER',folder)
    old=directory/'proof.json';old.write_text(json.dumps({'status':'failed','code':'invalid_input','paid_attempt_count':0,'attempts':[]}))
    claim=folder/'run-claim.json';claim.write_text(json.dumps({'run_id':old_id}))
    (folder/'setup.json').write_text('{}');authdir=folder/'retry-authorizations';authdir.mkdir()
    auth=dict(prior_run_id=old_id,new_run_id=new_id,prior_proof_sha256=hashlib.sha256(old.read_bytes()).hexdigest(),prior_claim_sha256=hashlib.sha256(claim.read_bytes()).hexdigest(),
              source_sha256=probe.fingerprint(),fixture_sha256=probe.fixture_hash(),lock_sha256=hashlib.sha256(Path('backend/uv.lock').read_bytes()).hexdigest(),setup_sha256=hashlib.sha256((folder/'setup.json').read_bytes()).hexdigest(),
              region=REGION,chat_model=CHAT_MODEL,embedding_model=EMBED_MODEL,max_paid_attempts=8,reservation_limit_usd='0.10',maximum_uses=1)
    path=authdir/'counttokens-retry.json';path.write_text(json.dumps(auth))
    return folder,old,claim,path,auth


@pytest.mark.parametrize('change',['proof_tamper','paid_attempt','source_change','lock_change','setup_change','wrong_model','larger_budget'])
def test_retry_authorization_rejects_changed_evidence_or_expanded_scope(tmp_path,monkeypatch,change):
    import hashlib
    folder,old,claim,path,auth=retry_fixture(tmp_path,monkeypatch)
    if change=='proof_tamper':old.write_text(old.read_text()+' ')
    if change=='paid_attempt':
        value=json.loads(old.read_text());value['paid_attempt_count']=1;old.write_text(json.dumps(value));auth['prior_proof_sha256']=hashlib.sha256(old.read_bytes()).hexdigest()
    if change=='source_change':auth['source_sha256']={}
    if change=='lock_change':auth['lock_sha256']='changed'
    if change=='setup_change':(folder/'setup.json').write_text('{"changed":true}')
    if change=='wrong_model':auth['chat_model']='other-model'
    if change=='larger_budget':auth['reservation_limit_usd']='1'
    path.write_text(json.dumps(auth))
    with pytest.raises(ModelFailure):probe.validate_retry(path)


def test_single_explicit_retry_preserves_original_claim_and_proof_and_cannot_repeat(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from types import SimpleNamespace
    folder,old,claim,path,auth=retry_fixture(tmp_path,monkeypatch);before=(old.read_bytes(),claim.read_bytes())
    monkeypatch.setattr(probe,'preflight',lambda:({},dict(ready=True)));children=[];barrier=Barrier(2)
    def spawned(command,**kwargs):
        children.append(command);p=Path(command[-1]);d=json.loads(p.read_text());d.update(passed=True,status='passed');p.write_text(json.dumps(d));return SimpleNamespace(returncode=0)
    monkeypatch.setattr(probe.subprocess,'run',spawned)
    def start():
        barrier.wait(timeout=3)
        try:probe.run(path);return True
        except ModelFailure:return False
    with ThreadPoolExecutor(max_workers=2) as pool:outcomes=list(pool.map(lambda _:start(),range(2)))
    assert sorted(outcomes)==[False,True] and len(children)==1
    assert (old.read_bytes(),claim.read_bytes())==before
    new=json.loads((folder/'runs'/auth['new_run_id']/'proof.json').read_text())
    assert new['retry_of_run_id']==auth['prior_run_id'] and new['prior_proof_sha256']==auth['prior_proof_sha256']
    with pytest.raises(ModelFailure):probe.run(path)


def test_observed_chat_usage_above_limit_is_kept_before_rejecting(tmp_path):
    p=proof(tmp_path);r=response();r['usage']={'inputTokens':4097,'outputTokens':4,'totalTokens':4101};a=BedrockModels(Client(r),p.reserve)
    with pytest.raises(ModelFailure):p.perform(a,lambda:chat(a))
    assert p.data['attempts'][0]['usage']['input_tokens']==4097
    assert p.data['attempts'][0]['actual_token_cost_usd'] is not None


def test_second_authorization_cannot_reuse_same_zero_attempt_failure(tmp_path,monkeypatch):
    from uuid import uuid4
    folder,old,claim,path,auth=retry_fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(probe,'preflight',lambda:(_ for _ in ()).throw(ModelFailure('preflight_failed')))
    with pytest.raises(ModelFailure):probe.run(path)
    other=dict(auth,new_run_id=uuid4().hex);second=path.with_name('another-authorization.json');second.write_text(json.dumps(other))
    with pytest.raises(ModelFailure):probe.run(second)
    assert len(list((folder/'retry-claims').glob('*.json')))==1


def test_reviewed_retry_worker_is_bound_to_authorization_and_consumption(tmp_path,monkeypatch):
    from types import SimpleNamespace
    folder,old,claim,path,auth=retry_fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(probe,'preflight',lambda:({},dict(ready=True)))
    class Runtime:
        def close(self):pass
    monkeypatch.setattr(probe.boto3,'client',lambda *args,**kwargs:Runtime())
    calls=[];monkeypatch.setattr(probe,'exercise',lambda *args:calls.append(True))
    def spawned(command,**kwargs):probe.worker(Path(command[-1]));return SimpleNamespace(returncode=0)
    monkeypatch.setattr(probe.subprocess,'run',spawned)
    probe.run(path)
    new=folder/'runs'/auth['new_run_id']/'proof.json'
    assert json.loads(new.read_text())['passed'] is True and calls==[True]
    with pytest.raises(ModelFailure):probe.worker(new)
    assert calls==[True]
