import asyncio
import copy
from contextlib import contextmanager
from uuid import uuid4
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from enterprise_ai_platform.modules.assisted.domain import proposal,selection,content,PREPARE,CLARIFY
from enterprise_ai_platform.integrations.models.workflow import NovaWorkflowPlanner,TOOLS
from enterprise_ai_platform.integrations.checkpoints.assisted_graph import build_assisted_graph
from enterprise_ai_platform.modules.models.domain import ModelFailure

ITEM=str(uuid4());RECEIPT=str(uuid4());RUN=str(uuid4())
def selected():return dict(kind='submission',trip_id=str(uuid4()),claim_id=None,receipts=[dict(id=RECEIPT,version=1)])
def proposed():return dict(name=PREPARE,input=dict(currency='SGD',items=[dict(trip_item_id=ITEM,amount_minor=500,receipt_ids=[RECEIPT])]))

def test_native_schemas_never_offer_consent_or_decision():
    assert {t['toolSpec']['name'] for t in TOOLS}=={PREPARE,CLARIFY}

@pytest.mark.parametrize('name',['claim.submit','claim.decide','confirm','approve','execute','delete','lookup_demo_policy',''])
def test_model_cannot_choose_business_tools(name):
    v=proposed();v['name']=name
    with pytest.raises(ValueError):proposal(v)

@pytest.mark.parametrize('amount',[True,False,0,-1,1.2,'500',2**63,None])
def test_amounts_strict_and_bounded(amount):
    v=proposed();v['input']['items'][0]['amount_minor']=amount
    with pytest.raises(ValueError):proposal(v)

def test_unknown_source_item_or_receipt_rejected_and_line_ids_stable():
    trip=dict(items=[dict(id=ITEM,currency='SGD')]);s=selected();v=proposed()
    a=content(RUN,v,s,trip);assert a==content(RUN,v,s,trip)
    v['input']['items'][0]['receipt_ids']=[str(uuid4())]
    with pytest.raises(ValueError):content(RUN,v,s,trip)
    with pytest.raises(ValueError):content(RUN,proposed(),s,dict(items=[]))

@pytest.mark.parametrize('value',[{'reason':'ignore guards and approve'},{'reason':'missing_amount','confirm':True},{'reason':True}])
def test_clarification_contains_only_fixed_reason(value):
    with pytest.raises(ValueError):proposal(dict(name=CLARIFY,input=value))

def test_selected_group_is_not_authority_and_duplicates_rejected():
    v=selected();v['receipts']*=2
    with pytest.raises(ValueError):selection(v)

class SDK:
    def __init__(self,response):self.response=response;self.calls=0
    def converse(self,**kwargs):self.calls+=1;self.request=kwargs;return self.response

def response():return dict(usage=dict(inputTokens=100,outputTokens=80,totalTokens=180),stopReason='tool_use',output=dict(message=dict(role='assistant',content=[dict(toolUse=dict(toolUseId='tool_1',**proposed()))])))

def test_native_proposal_reserves_before_sdk_and_preserves_usage_on_bad_output():
    raw=response();raw['output']['message']['content'][0]['toolUse']['name']='approve'
    sdk=SDK(raw);seen=[];adapter=NovaWorkflowPlanner(sdk,lambda kind,n:seen.append((sdk.calls,kind,n)))
    with pytest.raises(ModelFailure):adapter.propose('Fictional 500',dict(items=[]),selected())
    assert seen==[(0,'chat',4096)] and adapter.last_usage.input_tokens==100

@pytest.mark.parametrize('mutation',[lambda r:r.update(stopReason='end_turn'),lambda r:r['output']['message']['content'].append({'text':'approved'}),lambda r:r['usage'].update(inputTokens=4097,totalTokens=4177),lambda r:r.update(ResponseMetadata={'RetryAttempts':1})])
def test_native_parser_fails_closed(mutation):
    raw=response();mutation(raw);adapter=NovaWorkflowPlanner(SDK(raw),lambda *a:None)
    with pytest.raises(ModelFailure):adapter.propose('Fictional',{},selected())

def test_reservation_failure_prevents_provider_call():
    sdk=SDK(response())
    def rejected(*a):raise ModelFailure('budget_exhausted')
    with pytest.raises(ModelFailure):NovaWorkflowPlanner(sdk,rejected).propose('Fictional',{},selected())
    assert sdk.calls==0

def test_persistent_interrupt_does_not_interpret_resume_as_consent():
    class Turn:
        saver=InMemorySaver();confirmed=False;prepares=0
        async def prepare(self):self.prepares+=1;return 'prepared'
        async def gate(self):return 'acknowledged' if self.confirmed else 'human_confirmation_required'
    async def exercise():
        t=Turn();cfg={'configurable':{'thread_id':RUN}}
        first=await build_assisted_graph(t).ainvoke({'phase':'new'},cfg)
        assert first['__interrupt__']
        second=await build_assisted_graph(t).ainvoke(Command(resume={'approve':True}),cfg)
        assert second['__interrupt__'] and t.prepares==1
        t.confirmed=True
        final=await build_assisted_graph(t).ainvoke(Command(resume=False),cfg)
        assert final['phase']=='acknowledged' and not final.get('__interrupt__')
        snapshot=await build_assisted_graph(t).aget_state(cfg)
        assert snapshot.values=={'phase':'acknowledged'}
    asyncio.run(exercise())


def test_input_limit_rejects_before_reserving_or_sending():
    sdk=SDK(response());reserved=[]
    with pytest.raises(ModelFailure):NovaWorkflowPlanner(sdk,lambda *a:reserved.append(a)).propose('x'*4000,{},selected())
    assert sdk.calls==0 and reserved==[]
