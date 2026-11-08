"""Free test lane: mocked HTTP only, no credentials or provider traffic."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from enterprise_ai_platform.composition import build_openrouter_test_adapters
from enterprise_ai_platform.config import ConfigurationError
from enterprise_ai_platform.integrations.models.openrouter import FREE_MODEL, OpenRouterFreeChat
from enterprise_ai_platform.modules.models.domain import (
    Message, ModelFailure, PolicyResult, ToolCall, TOOL_NAME, Usage,
)
from enterprise_ai_platform.modules.rag.domain import context, sha
from enterprise_ai_platform.modules.rag.service import RagTurn

KEY = 'test-only-not-a-real-secret'


def response(text='Ready.', calls=None):
    return {'model': FREE_MODEL, 'choices': [{'finish_reason': 'tool_calls' if calls else 'stop',
        'message': {'role': 'assistant', 'content': text, 'tool_calls': calls or []}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 30, 'total_tokens': 130, 'cost': 0}}


def selected(name=TOOL_NAME, args=None):
    return {'id': 'call_fixture', 'type': 'function', 'function': {'name': name,
        'arguments': json.dumps(args or {'topic': 'travel', 'language': 'id'})}}


def setup(value=None, status=200, before=None, limit=6):
    sent = []
    def handle(request):
        sent.append(request)
        return httpx.Response(status, json=response() if value is None else value)
    client = httpx.Client(transport=httpx.MockTransport(handle))
    clock = Mock(return_value=100)
    adapter = OpenRouterFreeChat(client, KEY, before or Mock(), max_requests=limit, clock=clock)
    return adapter, sent, client, clock


def test_explicit_factory_pins_free_route_and_closes_client_without_aws(monkeypatch):
    monkeypatch.setattr('enterprise_ai_platform.composition.boto3.Session', Mock(side_effect=AssertionError('AWS forbidden')))
    seen = []
    transport = httpx.MockTransport(lambda request: (seen.append(request) or httpx.Response(200, json=response())))
    embedding = SimpleNamespace(embed=Mock())
    with build_openrouter_test_adapters(Mock(), api_key=KEY, embedding=embedding,
                                       fictional_only=True, transport=transport) as models:
        assert models.embedding is embedding
        assert models.chat.chat([Message('user', 'Fictional test')]).text == 'Ready.'
        owned_client = models.chat._client
    assert owned_client.is_closed
    request = seen[0]; payload = json.loads(request.content)
    assert str(request.url) == 'https://openrouter.ai/api/v1/chat/completions'
    assert payload['model'] == FREE_MODEL and payload['max_tokens'] == 256
    assert payload['provider'] == {'allow_fallbacks': False, 'require_parameters': True,
                                  'max_price': {'prompt': 0, 'completion': 0, 'request': 0}}
    assert not {'models', 'plugins', 'tools', 'tool_choice'} & payload.keys()
    assert KEY not in repr(models.chat)


@pytest.mark.parametrize('scope,embedding', [(False, SimpleNamespace(embed=Mock())), (True, None), (1, SimpleNamespace(embed=Mock()))])
def test_factory_rejects_implicit_scope_or_embedding(scope, embedding):
    with pytest.raises(ConfigurationError):
        with build_openrouter_test_adapters(Mock(), api_key=KEY, embedding=embedding, fictional_only=scope):
            pytest.fail('must not allocate')


@pytest.mark.parametrize('key,limit', [('', 6), ('short', 6), (KEY+'\n', 6), (KEY, True), (KEY, 11), (KEY, 0)])
def test_invalid_config_without_egress(key, limit):
    with pytest.raises(ModelFailure):
        OpenRouterFreeChat(Mock(), key, Mock(), max_requests=limit)


@pytest.mark.parametrize('status,code', [(429, 'rate_limited'), (401, 'access_denied'), (403, 'access_denied'),
                                       (402, 'budget_exhausted'), (404, 'provider_unavailable'), (503, 'provider_unavailable'),
                                       (302, 'provider_unavailable')])
def test_failure_mapping_no_retry_or_raw_error(status, code):
    a, sent, client, _ = setup({'error': {'message': KEY+' private upstream text'}}, status=status)
    with client, pytest.raises(ModelFailure) as error:
        a.chat([Message('user', 'Synthetic')])
    assert error.value.code == code and len(sent) == 1 and a.attempts == 1
    assert KEY not in str(error.value) and a.last_usage is None and a.last_cost_usd is None


def test_budget_rate_limit_and_failed_attempt_consumption():
    a, sent, client, clock = setup(limit=2)
    with client:
        a.chat([Message('user', 'Synthetic')])
        with pytest.raises(ModelFailure, match='rate_limited'):
            a.chat([Message('user', 'Synthetic')])
        clock.return_value = 104
        a.chat([Message('user', 'Synthetic')])
        clock.return_value = 108
        with pytest.raises(ModelFailure, match='budget_exhausted'):
            a.chat([Message('user', 'Synthetic')])
    assert len(sent) == 2


def test_authority_rejection_prevents_http_and_no_usage():
    a, sent, client, _ = setup(before=Mock(side_effect=ModelFailure('deadline')))
    with client, pytest.raises(ModelFailure, match='deadline'):
        a.chat([Message('user', 'Synthetic')])
    assert not sent and a.attempts == 0


@pytest.mark.parametrize('change', [
    {'model': 'paid/model'}, {'error': {'code': 500}}, {'choices': []},
    {'usage': {'prompt_tokens': True, 'completion_tokens': 30, 'total_tokens': 31, 'cost': 0}},
    {'usage': {'prompt_tokens': 100, 'completion_tokens': 30, 'total_tokens': 131, 'cost': 0}},
    {'usage': {'prompt_tokens': 100, 'completion_tokens': 30, 'total_tokens': 130}},
    {'usage': {'prompt_tokens': 100, 'completion_tokens': 30, 'total_tokens': 130, 'cost': 'NaN'}},
])
def test_response_model_usage_and_cost_validation(change):
    value=response();value.update(change);a, _, client, _ = setup(value)
    with client, pytest.raises(ModelFailure, match='invalid_output'):
        a.chat([Message('user', 'Synthetic')])


@pytest.mark.parametrize('finish,text,cost', [('length','partial',0), ('stop','',0), ('stop','Ready.',0.01)])
def test_failed_output_retains_known_usage_and_cost(finish,text,cost):
    value=response(text);value['choices'][0]['finish_reason']=finish;value['usage']['cost']=cost
    a, _, client, _=setup(value)
    with client, pytest.raises(ModelFailure):a.chat([Message('user','Synthetic')])
    assert a.last_usage==Usage(100,30) and float(a.last_cost_usd)==cost


@pytest.mark.parametrize('bad', [[], [Message('assistant','Synthetic')], [Message('user','x'*9000)],
                                  [Message('user','ok',tool=ToolCall('id','en'))]])
def test_invalid_input_has_no_observer_or_egress(bad):
    before=Mock();a,sent,client,_=setup(before=before)
    with client, pytest.raises(ModelFailure, match='invalid_input'):a.chat(bad)
    assert not sent;before.assert_not_called()


def test_real_application_tool_contract_and_result_serialization():
    a,sent,client,clock=setup(response(None,[selected()]))
    with client:
        reply=a.chat([Message('user','Read the fictional travel policy')],tools=True)
        assert reply.tool==ToolCall('call_fixture','id')
        first=json.loads(sent[0].content)
        assert first['tools'][0]['function']['name']==TOOL_NAME
        assert 'tool_choice' not in first
        clock.return_value=104
        # A controlled transport switches to a final textual response.
        client._transport=httpx.MockTransport(lambda request: (sent.append(request) or httpx.Response(200,json=response())))
        result=PolicyResult(reply.tool.id,7,'HR-01','Submit within seven days.')
        a.chat([Message('user','Read the fictional travel policy'),reply.message,Message('user',result=result)],tools=True)
        final=json.loads(sent[-1].content)
        assert not {'tools','tool_choice'} & final.keys()
        assert final['messages'][-1]['role']=='tool'
        assert json.loads(final['messages'][-1]['content'])['source']=='HR-01'


@pytest.mark.parametrize('calls', [[selected('delete_all')], [selected(args={'topic':'pay','language':'id'})],
                                 [selected(),selected()], [selected(args={'topic':'travel','language':'id','execute':True})]])
def test_untrusted_tools_fail_closed(calls):
    a,_,client,_=setup(response(None,calls))
    with client, pytest.raises(ModelFailure):a.chat([Message('user','Synthetic')],tools=True)


def test_tool_output_forbidden_without_tools():
    a,_,client,_=setup(response(None,[selected()]))
    with client, pytest.raises(ModelFailure):a.chat([Message('user','Synthetic')])


def test_timeout_body_size_and_duplicate_json_are_sanitized():
    for handler,code in [
        (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout(KEY)), 'timeout'),
        (lambda r: httpx.Response(200,content=b'x'*262145), 'invalid_output'),
        (lambda r: httpx.Response(200,content=b'{"model":1,"model":2}'), 'invalid_output'),
    ]:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            a=OpenRouterFreeChat(client,KEY,Mock())
            with pytest.raises(ModelFailure) as error:a.chat([Message('user','Synthetic')])
            assert error.value.code==code and KEY not in str(error.value)


@pytest.mark.parametrize('change', [{'model': 'wrong/model'}, {'error': {'code': 500}}])
def test_accounting_retained_before_rejecting_model_or_error(change):
    value=response();value.update(change);a,_,client,_=setup(value)
    with client,pytest.raises(ModelFailure):a.chat([Message('user','Synthetic')])
    assert a.last_usage==Usage(100,30) and a.last_cost_usd=='0'


def test_nonzero_cost_latches_run_and_prevents_more_egress():
    value=response();value['usage']['cost']=0.01
    before=Mock();a,sent,client,clock=setup(value,before=before)
    with client:
        with pytest.raises(ModelFailure,match='invalid_output'):a.chat([Message('user','Synthetic')])
        clock.return_value=110
        with pytest.raises(ModelFailure,match='budget_exhausted'):a.chat([Message('user','Synthetic')])
    assert len(sent)==1 and before.call_count==1 and a.last_cost_usd=='0.01'


def test_slow_drip_has_wall_deadline_and_closes_response():
    tick=[100];closed=[]
    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(10):
                tick[0]+=6
                yield b' '
        def close(self):closed.append(True)
    transport=httpx.MockTransport(lambda r:httpx.Response(200,stream=Drip()))
    with httpx.Client(transport=transport) as client:
        a=OpenRouterFreeChat(client,KEY,Mock(),clock=lambda:tick[0])
        with pytest.raises(ModelFailure,match='timeout'):a.chat([Message('user','Synthetic')])
    assert tick[0]==124 and closed and a.last_usage is None


def test_full_graph_smoke_with_controlled_http_no_aws_or_key(tmp_path,monkeypatch):
    import importlib.util
    from pathlib import Path
    path=Path(__file__).resolve().parents[3]/'scripts/checks/check_openrouter_local.py'
    spec=importlib.util.spec_from_file_location('openrouter_smoke_test',path)
    smoke=importlib.util.module_from_spec(spec);spec.loader.exec_module(smoke)
    monkeypatch.setattr(smoke.logging,'disable',lambda _:None)
    monkeypatch.setattr(smoke,'ROOT',tmp_path);monkeypatch.setattr(smoke,'SOURCES',[])
    monkeypatch.setattr(smoke,'dotenv_values',lambda _: {'OPENROUTER_API_KEY':KEY})
    original=smoke.build_openrouter_test_adapters
    sent=[]
    def handle(request):
        sent.append(request)
        output=({'kind':'answer','citations':[{'id':'S1','quote':'Karyawan tetap mendapat 12 hari cuti tahunan.'}]}
                if len(sent)==1 else {'kind':'insufficient_evidence','citations':[]})
        return httpx.Response(200,json=response(json.dumps(output)))
    def factory(*args,**kwargs):return original(*args,**kwargs,transport=httpx.MockTransport(handle))
    monkeypatch.setattr(smoke,'build_openrouter_test_adapters',factory)
    assert asyncio.run(smoke.run())==0
    proofs=list(tmp_path.glob('local/openrouter-integration/*/proof.json'))
    proof=json.loads(proofs[0].read_text())
    assert proof['passed'] and len(sent)==2 and len(proof['reservations'])==4
    assert all(c['http_attempts']==1 and c['last_cost_usd']=='0' and len(c['settlements'])==2 for c in proof['cases'])
    assert KEY not in proofs[0].read_text()


def test_real_rag_generate_uses_observer_parses_quotes_and_settles_usage():
    text='Travel claims must be submitted within 7 calendar days.'
    hit=dict(version_id='fixture-version',version_no=1,index_generation=1,access_revision=1,
             chunk_no=0,page=1,start=0,text=text,text_digest=sha(text))
    output=json.dumps({'kind':'answer','citations':[{'id':'S1','quote':text}]})
    async def run():
        repo=SimpleNamespace(call=AsyncMock(return_value={'authority_expires_at':10**12,'settlement_receipt':'a'*64}),
                             runtime=AsyncMock(),settle=AsyncMock())
        turn=RagTurn(repo,None,None,None,None);turn.ctx=context('Deadline?','en',[hit]);turn.language='en'
        a,_,client,_=setup(response(output),before=turn.observer(asyncio.get_running_loop()))
        with client:await turn.generate(a)
        assert turn.buffer['kind']=='answer' and turn.buffer['citations'][0]['text_digest']==sha(text)
        assert any(c.args[0]=='reserve' for c in repo.call.call_args_list)
        repo.settle.assert_awaited_once_with('chat','a'*64,Usage(100,30))
    asyncio.run(run())
