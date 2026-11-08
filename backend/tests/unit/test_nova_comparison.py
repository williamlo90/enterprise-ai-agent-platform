import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock
from decimal import Decimal

from botocore.exceptions import ClientError
import pytest

spec=importlib.util.spec_from_file_location('nova_comparison',Path(__file__).resolve().parents[3]/'scripts/spikes/models/compare_nova.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def response(text='{"kind":"answer","days":7,"source":"A-policy-v1","answer":"7 hari."}'):
    return {'output':{'message':{'role':'assistant','content':[{'text':text}]}},'stopReason':'end_turn',
            'usage':{'inputTokens':100,'outputTokens':20,'totalTokens':120},'ResponseMetadata':{'RetryAttempts':0}}


def trial(tmp_path, client=None):
    proof={'attempts':[],'results':[]}
    return m.Trial(client or Mock(),tmp_path/'proof.json',proof)


def test_fixture_counts_and_maximum_reservation():
    assert len(m.CASES)==8 and sum(c['type']=='tool' for c in m.CASES)==2
    assert (6+2*2)*3*3==90
    assert sum((Decimal(4096)*Decimal(a)+Decimal(256)*Decimal(b))*30/1000000 for a,b in m.RATES.values())==Decimal('.1855488')


@pytest.mark.parametrize('case,value',[(m.CASES[0],{'kind':'answer','days':7,'source':'A-policy-v1','answer':'7 hari.'}),(m.CASES[2],{'kind':'clarify','field':'currency','question':'Mata uang apa?'}),(m.CASES[4],{'kind':'abstain','reason':'Sumber tidak mencantumkan batas nominal.'})])
def test_rubric_positive(case,value): assert m.assess(json.dumps(value),case)


@pytest.mark.parametrize('value',[{'kind':'answer','days':14,'source':'A-policy-v1','answer':'14 hari.'},{'kind':'answer','days':True,'source':'A-policy-v1','answer':'7 hari.'},{'kind':'answer','days':7,'source':'other','answer':'7 hari.'},{'kind':'answer','days':7,'source':'A-policy-v1','answer':'7 hari.','extra':'hidden'}])
def test_rubric_rejects_material_or_schema_errors(value): assert not m.assess(json.dumps(value),m.CASES[0])


def test_duplicate_json_rejected():
    with pytest.raises(ValueError):m.strict('{"days":7,"days":14}')


def test_json_only_contract_rejects_markdown_and_thinking():
    text='{"kind":"answer","days":7,"source":"A-policy-v1","answer":"7 hari."}'
    assert m.assess(text,m.CASES[0])
    assert not m.assess('```json\n'+text+'\n```',m.CASES[0])
    assert not m.assess('<thinking>private</thinking>'+text,m.CASES[0])


def test_reserve_is_durable_before_network_and_no_retry(tmp_path):
    c=Mock();t=trial(tmp_path,c)
    def reply(**kwargs):
        disk=json.loads(t.path.read_text(encoding='utf-8'));assert disk['attempts'][0]['state']=='reserved_outcome_unknown'
        assert kwargs['modelId']=='apac.amazon.nova-lite-v1:0'
        assert 'additionalModelRequestFields' not in kwargs
        return response()
    c.converse.side_effect=reply
    r=t.task('lite',m.CASES[0],1)
    assert r['passed'] and c.converse.call_count==1


def test_usage_preserved_after_content_schema_failure(tmp_path):
    c=Mock();v=response();v['output']['message']['role']='user';c.converse.return_value=v;t=trial(tmp_path,c)
    r=t.task('micro',m.CASES[0],1)
    assert not r['passed'] and t.proof['attempts'][0]['usage']['input_tokens']==100
    assert t.proof['attempts'][0]['cost_usd'] is not None


def test_unknown_usage_is_not_zero(tmp_path):
    c=Mock();v=response();del v['usage'];c.converse.return_value=v;t=trial(tmp_path,c)
    assert t.task('lite',m.CASES[0],1)['result']=='usage_unknown'
    assert t.proof['attempts'][0]['cost_usd'] is None


def test_error_message_never_persisted(tmp_path):
    c=Mock();c.converse.side_effect=ClientError({'Error':{'Code':'AccessDeniedException','Message':'SECRET_PROVIDER_DETAILS'}},'Converse');t=trial(tmp_path,c)
    assert t.task('pro',m.CASES[0],1)['result']=='provider_error'
    assert c.converse.call_count==1 and 'SECRET_PROVIDER_DETAILS' not in t.path.read_text()


def test_tool_roundtrip_exact_binding(tmp_path):
    c=Mock();first=response();first['stopReason']='tool_use';first['output']['message']['content']=[{'toolUse':{'toolUseId':'demo-1','name':'lookup_demo_policy','input':{'topic':'travel','language':'id'}}}]
    c.converse.side_effect=[first,response('{"kind":"answer","days":7,"source":"demo-policy-v1","answer":"7 hari."}')]
    t=trial(tmp_path,c);r=t.task('lite',m.CASES[6],1)
    assert r['passed'] and r['tool_contract_passed'] and c.converse.call_count==2
    assert c.converse.call_args.kwargs['messages'][-1]['content'][0]['toolResult']['toolUseId']=='demo-1'


def test_invalid_tool_never_executes_or_continues(tmp_path):
    c=Mock();v=response();v['stopReason']='tool_use';v['output']['message']['content']=[{'toolUse':{'toolUseId':'demo-1','name':'delete_everything','input':{}}}];c.converse.return_value=v
    assert trial(tmp_path,c).task('lite',m.CASES[6],1)['result']=='tool_arguments_invalid'
    assert c.converse.call_count==1


def test_budget_and_input_guard_before_network(tmp_path):
    c=Mock();t=trial(tmp_path,c)
    with pytest.raises(ValueError,match='input_size_limit'):t.call('lite','x',1,[{'role':'user','content':[{'text':'x'*4000}]}])
    t.proof['attempts']=[{'model':'pro','reserved_usd':'.25'}]
    with pytest.raises(ValueError,match='budget_limit'):t.call('lite','x',1,[{'role':'user','content':[{'text':'x'}]}])
    c.converse.assert_not_called()
