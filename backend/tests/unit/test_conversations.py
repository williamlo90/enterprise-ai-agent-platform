import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import httpx
import pytest
from enterprise_ai_platform.modules.conversations.domain import ConversationFailure,SourceRef,public_messages
from enterprise_ai_platform.modules.conversations.service import ConversationService,DenySources
from enterprise_ai_platform.infrastructure.database.conversations import PostgresConversationRepository
from enterprise_ai_platform.composition import ApplicationServices
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.main import create_app

ORIGIN='https://conv.example'
def headers():return dict(origin=ORIGIN,**{'content-type':'application/json','x-csrf-token':'c'*43})
def ref(**kwargs):return SourceRef(**dict(system='service',kind='claim',resource_id=str(uuid4()),version='1',relation='active_reviewer',observed_revision='1',**kwargs))
def source_dict(value):return {name:getattr(value,name) for name in value.__dataclass_fields__}
def message(*,mode='source_free',kind='model',sources=None):
 return dict(id=str(uuid4()),sequence=2,author_kind=kind,run_id=str(uuid4()),output_kind='answer',created_at='2026-11-04',provenance_mode=mode,body='PRIVATE-BODY',_sources=sources or [],_snapshot='a'*64,feedback=dict(reason='PRIVATE-FEEDBACK'))

@pytest.mark.parametrize('method,values',[
 ('create',dict(language='xx',key=str(uuid4()))),('create',dict(language='id',key='bad')),('create',dict(language='en',key=str(uuid4()),owner_id=str(uuid4()))),
 ('append',dict(text=' ',client_message_id=str(uuid4()))),('append',dict(text='x'*4097,client_message_id=str(uuid4()))),('append',dict(text='\ud800',client_message_id=str(uuid4()))),
 ('append',dict(text='bad\x00text',client_message_id=str(uuid4()))),('append',dict(text='x',client_message_id='bad')),('append',dict(text='x',client_message_id=str(uuid4()),author_kind='model')),
 ('feedback',dict(key=str(uuid4()),rating='excellent',reason=None)),('feedback',dict(key='bad',rating='up',reason=None)),('feedback',dict(key=str(uuid4()),rating='up',reason='x'*1001)),
])
def test_input_cannot_supply_authority_or_unbounded_content(method,values):
 repo=SimpleNamespace(call=AsyncMock());service=ConversationService(repo,ORIGIN)
 args=('s'*43,headers(),values) if method=='create' else ('s'*43,headers(),str(uuid4()),values)
 with pytest.raises(ConversationFailure):asyncio.run(getattr(service,method)(*args))
 repo.call.assert_not_called()


def test_redaction_is_whole_message_and_does_not_leave_feedback_or_source_ids():
 source=ref();data=message(mode='dependent',sources=[source_dict(source)])
 result=public_messages([data],{source:False})[0]
 assert result['redacted'] and result['text'] is None and result['sources']==[] and result['feedback'] is None
 assert 'PRIVATE' not in str(result) and source.resource_id not in str(result)
 assert public_messages([data],{source:True})[0]['text']=='PRIVATE-BODY'
 for mode in ('unverified','dependent'):assert public_messages([message(mode=mode)],{})[0]['redacted']
 assert not public_messages([message(kind='human')],{})[0]['redacted']


def test_source_free_label_does_not_override_attached_run_sources():
 source=ref();data=message(mode='source_free',sources=[source_dict(source)])
 assert public_messages([data],{})[0]['redacted']
 assert 'PRIVATE' not in repr(source) and source.resource_id not in repr(source)

@pytest.mark.parametrize('change',[dict(system='url'),dict(kind='arbitrary'),dict(relation='admin'),dict(version='https://secret'),dict(page=True),dict(chunk=32)])
def test_typed_reference_rejects_unrecognized_sources(change):
 value=dict(system='service',kind='claim',resource_id=str(uuid4()),version='1',relation='active_reviewer',observed_revision='1');value.update(change)
 with pytest.raises(ConversationFailure):SourceRef(**value)


def history_fixture(decisions=None,fail_guard=False):
 events=[];m=message(mode='dependent',sources=[source_dict(ref())]);cid=str(uuid4())
 @asynccontextmanager
 async def tx():
  events.append('begin')
  try:yield object()
  except BaseException:events.append('rollback');raise
  else:events.append('commit')
 @asynccontextmanager
 async def hold(subject,sources):
  events.append('source_enter')
  try:
   if fail_guard:raise TimeoutError()
   yield decisions if decisions is not None else tuple(True for _ in sources)
  finally:events.append('source_exit')
 repo=PostgresConversationRepository(SimpleNamespace(transaction=tx))
 row=dict(conversation=dict(id=cid,version=2),messages=[m],next_after=None,tenant_id=str(uuid4()),principal_id=str(uuid4()),authority_expires_at=20)
 repo.execute=AsyncMock(side_effect=[dict(row),dict(row)])
 return repo,SimpleNamespace(hold=hold),cid,events


def test_source_guard_remains_held_through_owner_read_commit():
 repo,sources,cid,events=history_fixture();result=asyncio.run(repo.history('token',dict(id=cid,after=0,limit=20),sources))
 assert result['messages'][0]['text']=='PRIVATE-BODY' and events==['begin','source_enter','commit','source_exit']
 assert '_snapshot' not in result['messages'][0] and '_sources' not in result['messages'][0]

@pytest.mark.parametrize('decisions',[[],(1,),(True,False)])
def test_malformed_source_permit_fails_closed_and_rolls_back(decisions):
 repo,sources,cid,events=history_fixture(decisions=decisions)
 with pytest.raises(ConversationFailure):asyncio.run(repo.history('token',dict(id=cid,after=0,limit=20),sources))
 assert 'rollback' in events and 'commit' not in events


def test_source_outage_returns_no_buffer_and_rolls_back():
 repo,sources,cid,events=history_fixture(fail_guard=True)
 with pytest.raises(ConversationFailure):asyncio.run(repo.history('token',dict(id=cid,after=0,limit=20),sources))
 assert 'rollback' in events and 'commit' not in events


def test_cancellation_releases_source_guard_and_owner_transaction():
 async def run():
  repo,_,cid,events=history_fixture();entered=asyncio.Event()
  @asynccontextmanager
  async def hold(subject,sources):
   try:entered.set();await asyncio.Event().wait();yield ()
   finally:events.append('source_released')
  task=asyncio.create_task(repo.history('token',dict(id=cid,after=0,limit=20),SimpleNamespace(hold=hold)))
  await entered.wait();task.cancel()
  with pytest.raises(asyncio.CancelledError):await task
  assert events==['begin','source_released','rollback']
 asyncio.run(run())


def test_post_commit_deadline_rejects_history_response():
 repo=SimpleNamespace(history=AsyncMock(return_value=dict(authority_expires_at=10,messages=[])))
 service=ConversationService(repo,ORIGIN,clock=lambda:10)
 with pytest.raises(ConversationFailure) as error:asyncio.run(service.history('s'*43,str(uuid4())))
 assert error.value.status==401


def test_http_owner_fields_authors_cursor_and_private_error_headers():
 instance=SimpleNamespace(create=AsyncMock(return_value=dict(id=str(uuid4()))),list=AsyncMock(return_value=dict(items=[])),append=AsyncMock(return_value=dict(run_state='queued')),history=AsyncMock(return_value=dict(messages=[])),feedback=AsyncMock(return_value=dict(feedback_version=1)))
 async def run():
  app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=SimpleNamespace(),conversations=instance))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
   h={**headers(),'cookie':'__Host-eai-session='+'s'*43};cid=str(uuid4())
   assert (await client.post('/api/v1/conversations',json=dict(language='id',key=str(uuid4())),headers=h)).status_code==201
   response=await client.post('/api/v1/conversations',json=dict(language='en',key=str(uuid4()),owner_id=cid),headers=h)
   assert response.status_code==422 and response.headers['cache-control']=='no-store'
   assert (await client.post('/api/v1/conversations/'+cid+'/messages',json=dict(text='hello',client_message_id=str(uuid4())),headers=h)).status_code==202
   for query in ('limit=2&limit=3','tenant_id='+cid,'limit=auto'):
    assert (await client.get('/api/v1/conversations?'+query,headers=h)).status_code==400
   for query in ('after=-1','after=01','limit=21','after=0&tenant=x'):
    assert (await client.get('/api/v1/conversations/'+cid+'/messages?'+query,headers=h)).status_code==400
   assert (await client.get('/api/v1/conversations/'+cid+'/messages?after=0&limit=2',headers=h)).status_code==200
   assert instance.history.call_args.args[2:]==(0,2)
   assert (await client.post('/api/v1/messages/'+cid+'/feedback',json=dict(key=str(uuid4()),rating='down'),headers=h)).status_code==200
 asyncio.run(run())
