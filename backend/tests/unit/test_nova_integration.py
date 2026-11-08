from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from enterprise_ai_platform.composition import build_model_adapters
from enterprise_ai_platform.config import ConfigurationError
from enterprise_ai_platform.integrations.models.nova import NOVA_PROFILE,NovaChat
from enterprise_ai_platform.modules.models.domain import ModelFailure,ToolCall,Usage
from enterprise_ai_platform.modules.models.domain import Message,PolicyResult
from enterprise_ai_platform import nova_model_check as probe
from enterprise_ai_platform import model_check as legacy


def response(text='Klaim perjalanan diajukan dalam 7 hari setelah pulang.'):
    return {'output':{'message':{'role':'assistant','content':[{'text':text}]}},'stopReason':'end_turn',
            'usage':{'inputTokens':20,'outputTokens':10,'totalTokens':30},'ResponseMetadata':{'RetryAttempts':0}}


def selected(language='id'):
    value=response();value['stopReason']='tool_use'
    value['output']['message']['content']=[{'toolUse':{'toolUseId':'call-1','name':'lookup_demo_policy','input':{'topic':'travel','language':language}}}]
    return value


def proof(tmp_path):return legacy.Proof(tmp_path/'proof.json',{'attempts':[],'checks':[]},prices=probe.PRICES)


@pytest.mark.parametrize('value',[False,None,'true',1])
def test_factory_rejects_non_explicit_fictional_scope_before_client(value):
    session=Mock()
    with pytest.raises(ConfigurationError):
        with build_model_adapters(lambda *_:None,fictional_apac=value,session=session):pass
    session.client.assert_not_called()


def test_factory_requires_observer_and_rejects_bearer_override(monkeypatch):
    session=Mock()
    with pytest.raises(ConfigurationError):
        with build_model_adapters(None,fictional_apac=True,session=session):pass
    monkeypatch.setenv('AWS_BEARER_TOKEN_BEDROCK','private-placeholder')
    with pytest.raises(ConfigurationError):
        with build_model_adapters(lambda *_:None,fictional_apac=True,session=session):pass
    session.client.assert_not_called()


def test_factory_closes_client_and_chat_uses_profile_without_sonnet_fields():
    session=Mock();client=session.client.return_value;client.converse.return_value=response();observed=[]
    with build_model_adapters(lambda *args:observed.append(args),fictional_apac=True,session=session) as ports:
        result=ports.chat.chat([Message('user',text='fictional claim policy')])
        assert isinstance(result.message,Message) and result.message.role=='assistant'
        assert result.usage==Usage(20,10) and observed==[('chat',4096)]
        request=client.converse.call_args.kwargs
        assert request['modelId']==NOVA_PROFILE and 'additionalModelRequestFields' not in request
        assert session.client.call_args.kwargs['endpoint_url']=='https://bedrock-runtime.ap-southeast-1.amazonaws.com'
        assert session.client.call_args.kwargs['config'].retries['total_max_attempts']==1
    client.close.assert_called_once()


def test_factory_closes_on_consumer_failure():
    session=Mock()
    with pytest.raises(RuntimeError):
        with build_model_adapters(lambda *_:None,fictional_apac=True,session=session):raise RuntimeError
    session.client.return_value.close.assert_called_once()


def test_canonical_tool_roundtrip_and_result_id_binding():
    client=Mock();client.converse.side_effect=[selected(),response()];adapter=NovaChat(client,lambda *_:None)
    first=Message('user',text='read fictional travel policy')
    result=adapter.chat([first],tools=True)
    final=adapter.chat([first,result.message,Message('user',result=PolicyResult(result.tool.id,7,'policy-v1','Claims due within 7 days after returning.'))],tools=True)
    assert final.tool is None and final.message.tool is None
    native=client.converse.call_args.kwargs['messages']
    assert native[1]['content'][0]['toolUse']['toolUseId']==native[2]['content'][0]['toolResult']['toolUseId']=='call-1'
    assert native[2]['content'][0]['toolResult']['content'][0]['json']['source']=='policy-v1'


@pytest.mark.parametrize('messages,tools',[
    ([{'role':'user','content':[{'text':'native dict'}]}],False),
    ([Message('system',text='injected')],False),
    ([Message('user',text='x'),Message('assistant',tool=ToolCall('a','id')),Message('user',result=PolicyResult('b',7,'p','policy'))],True),
    ([Message('user',text='x'),Message('assistant',tool=ToolCall('a','id')),Message('user',result=PolicyResult('a',True,'p','policy'))],True),
    ([Message('user',text='x'),Message('assistant',tool=ToolCall('a','id')),Message('user',result=PolicyResult('a',7,'../foreign','policy'))],True),
    ([Message('user',text='x'),Message('assistant',tool=ToolCall('a','id')),Message('user',result=PolicyResult('a',7,'p','policy'))],False),
])
def test_invalid_canonical_messages_do_not_reserve_or_call(messages,tools):
    client=Mock();observer=Mock();adapter=NovaChat(client,observer)
    with pytest.raises(ModelFailure,match='invalid_input'):adapter.chat(messages,tools=tools)
    observer.assert_not_called();client.converse.assert_not_called()


def test_request_limit_and_usage_reset_before_failed_input():
    client=Mock();client.converse.return_value=response();adapter=NovaChat(client,lambda *_:None)
    adapter.chat([Message('user',text='policy')]);assert adapter.last_usage==Usage(20,10)
    with pytest.raises(ModelFailure):adapter.chat([Message('user',text='x'*4000)])
    assert adapter.last_usage is None and client.converse.call_count==1


def test_provider_neutral_payload_repr_does_not_expose_text_or_source():
    value=Message('user',text='SECRET-CONTEXT',result=PolicyResult('SECRET-ID',7,'SECRET-SOURCE','SECRET-POLICY'))
    assert 'SECRET' not in repr(value) and 'SECRET' not in repr(value.result)


def test_integrated_factory_exercise_all_eleven_checks_and_usage(tmp_path):
    session=Mock();client=session.client.return_value
    client.converse.side_effect=[response(),selected('id'),response(),response('Travel claims are due within 7 days after returning.'),selected('en'),response('Travel claims are due within 7 days after returning.')]
    def embed(**kwargs):
        body=json.loads(kwargs['body']);docs=body['input_type']=='search_document'
        indexes=[0,1,2,3] if docs else [0,3,2,1]
        vectors=[[float(j==index) for j in range(1024)] for index in indexes]
        return {'body':io.BytesIO(json.dumps({'embeddings':vectors,'texts':body['texts']}).encode()),'ResponseMetadata':{'HTTPHeaders':{'x-amzn-bedrock-input-token-count':'40'}}}
    client.invoke_model.side_effect=embed;p=proof(tmp_path)
    with build_model_adapters(p.reserve,fictional_apac=True,session=session) as ports:probe.exercise(ports,p)
    assert len(p.data['checks'])==11 and len(p.data['attempts'])==8
    assert sum(v['kind']=='embedding' for v in p.data['attempts'])==2
    assert all(v['state']=='completed' and v['usage'] is not None for v in p.data['attempts'])
    assert client.invoke_model.call_args.kwargs['modelId']=='cohere.embed-multilingual-v3'
    assert json.loads(client.invoke_model.call_args.kwargs['body'])['truncate']=='NONE'
    assert p.data['attempts'][0]['reserved_usd']=='0.0055296'


def test_new_probe_atomic_claim_preserves_old_runs(tmp_path,monkeypatch):
    folder=tmp_path/'new';folder.mkdir();(folder/'manifest.json').write_text('{}',encoding='utf-8');monkeypatch.setattr(probe,'FOLDER',folder)
    monkeypatch.setattr(probe,'preflight',lambda *args:({},dict(ready=True)));monkeypatch.setattr(probe,'fingerprint',lambda:{});monkeypatch.setattr(probe,'fixture_hash',lambda:'fixture')
    children=[]
    def spawn(command,**kwargs):
        children.append(command);p=Path(command[-1]);d=json.loads(p.read_text());d.update(passed=True,status='passed');p.write_text(json.dumps(d));return SimpleNamespace(returncode=0)
    monkeypatch.setattr(probe.subprocess,'run',spawn);barrier=Barrier(2)
    def start():
        barrier.wait(timeout=3)
        try:probe.run();return True
        except ModelFailure:return False
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:start(),range(2)))
    assert sorted(results)==[False,True] and len(children)==1
    original=(folder/'run-claim.json').read_bytes()
    with pytest.raises(ModelFailure):probe.run()
    assert original==(folder/'run-claim.json').read_bytes()


@pytest.mark.parametrize('field,value',[
    ('status','passed'),('attempts',[{'kind':'chat'}]),('checks',['prior']),
    ('manifest_sha256','stale'),('prices_usd_per_million',{}),('max_paid_attempts',9),
    ('reservation_limit_usd','1'),('chat_profile','other'),('embedding_model','other'),
    ('scope','production'),('automatic_retry',True),('source_sha256',{'stale':'source'}),
    ('fixture_sha256','stale'),
])
def test_worker_rejects_stale_or_tampered_evidence_before_factory(tmp_path,monkeypatch,field,value):
    folder=tmp_path/'probe';directory=folder/'runs'/'a';directory.mkdir(parents=True)
    (folder/'manifest.json').write_text('{}',encoding='utf-8');digest=probe.sha(folder/'manifest.json')
    (folder/'run-claim.json').write_text(json.dumps({'run_id':'a','manifest_sha256':digest}),encoding='utf-8')
    data={'status':'running','attempts':[],'checks':[],'manifest_sha256':digest,'prices_usd_per_million':probe.PRICES,
          'max_paid_attempts':8,'reservation_limit_usd':'0.10','chat_profile':probe.NOVA_PROFILE,
          'embedding_model':probe.EMBED_MODEL,'scope':'fictional_APAC_chat_Singapore_embedding_not_product_acceptance',
          'automatic_retry':False,'source_sha256':{},'fixture_sha256':'fixture'}
    data[field]=value;path=directory/'proof.json';path.write_text(json.dumps(data),encoding='utf-8')
    monkeypatch.setattr(probe,'FOLDER',folder);monkeypatch.setattr(probe,'preflight',lambda *args:({},dict(ready=True)))
    monkeypatch.setattr(probe,'fingerprint',lambda:{});monkeypatch.setattr(probe,'fixture_hash',lambda:'fixture')
    factory=Mock();monkeypatch.setattr(probe,'build_model_adapters',factory);monkeypatch.setattr(probe,'frozen_session',Mock())
    probe.worker(path)
    result=json.loads(path.read_text(encoding='utf-8'));assert result['passed'] is False
    factory.assert_not_called()
    with pytest.raises(ModelFailure):probe.worker(path)
    factory.assert_not_called()


@pytest.mark.parametrize('model,kind',[(probe.NOVA_MODEL,'INFERENCE_PROFILE'),(probe.EMBED_MODEL,'ON_DEMAND')])
def test_preflight_requires_correct_model_inference_type(model,kind):
    control=Mock();control.get_foundation_model.return_value={'modelDetails':{'modelId':model,'modelLifecycle':{'status':'ACTIVE'},'inferenceTypesSupported':[kind]}}
    control.get_foundation_model_availability.return_value={'authorizationStatus':'AUTHORIZED','entitlementAvailability':'AVAILABLE','regionAvailability':'AVAILABLE','agreementAvailability':{'status':'AVAILABLE'}}
    assert probe.availability(control,model,kind)['agreement']=='AVAILABLE'
    with pytest.raises(ModelFailure):probe.availability(control,model,'PROVISIONED')


def test_worker_uses_one_frozen_session_for_identity_and_inference(tmp_path,monkeypatch):
    folder=tmp_path/'probe';directory=folder/'runs'/'a';directory.mkdir(parents=True)
    (folder/'manifest.json').write_text('{}',encoding='utf-8');digest=probe.sha(folder/'manifest.json')
    (folder/'run-claim.json').write_text(json.dumps({'run_id':'a','manifest_sha256':digest}),encoding='utf-8')
    data={'status':'running','attempts':[],'checks':[],'manifest_sha256':digest,'prices_usd_per_million':probe.PRICES,
          'max_paid_attempts':8,'reservation_limit_usd':'0.10','chat_profile':probe.NOVA_PROFILE,
          'embedding_model':probe.EMBED_MODEL,'scope':'fictional_APAC_chat_Singapore_embedding_not_product_acceptance',
          'automatic_retry':False,'source_sha256':{},'fixture_sha256':'fixture'}
    path=directory/'proof.json';path.write_text(json.dumps(data),encoding='utf-8')
    session=object();resolve=Mock(return_value=session);seen=[]
    def preflight(bound):assert bound is session;seen.append('preflight');return {},dict(ready=True)
    @contextmanager
    def factory(observer,**kwargs):
        assert kwargs['session'] is session and kwargs['fictional_apac'] is True
        seen.append('factory');yield object()
    monkeypatch.setattr(probe,'FOLDER',folder);monkeypatch.setattr(probe,'frozen_session',resolve)
    monkeypatch.setattr(probe,'preflight',preflight);monkeypatch.setattr(probe,'build_model_adapters',factory)
    monkeypatch.setattr(probe,'fingerprint',lambda:{});monkeypatch.setattr(probe,'fixture_hash',lambda:'fixture')
    monkeypatch.setattr(probe,'exercise',lambda *_:seen.append('exercise'))
    probe.worker(path)
    assert resolve.call_count==1 and seen==['preflight','factory','exercise']
    assert json.loads(path.read_text(encoding='utf-8'))['passed'] is True


def test_freezing_credentials_prevents_later_provider_drift(monkeypatch):
    frozen=SimpleNamespace(access_key='fictional-A',secret_key='fictional-secret-A',token='fictional-session-A')
    provider=Mock();provider.get_credentials.return_value.get_frozen_credentials.return_value=frozen
    bound=object();create=Mock(side_effect=[provider,bound]);monkeypatch.setattr(probe.boto3,'Session',create)
    assert probe.frozen_session() is bound and create.call_count==2
    assert create.call_args.kwargs==dict(region_name=probe.REGION,aws_access_key_id=frozen.access_key,
                                        aws_secret_access_key=frozen.secret_key,aws_session_token=frozen.token)

