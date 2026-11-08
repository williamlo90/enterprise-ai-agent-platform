import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from langgraph.types import Interrupt
from enterprise_ai_platform.integrations.checkpoints.langgraph import BoundCheckpointSaver,encode,decode
from enterprise_ai_platform.infrastructure.database.checkpoints import PostgresCheckpointRepository
from enterprise_ai_platform.modules.conversations.ports import ConversationFailure


@pytest.mark.parametrize('value',[object(),b'bytes',{'password':'bad'},{'$type':'pickle'},{'token':'bad'},float('nan'),float('inf'),{1:'bad'},[[[[object()]]]]])
def test_codec_rejects_executable_unbounded_nonfinite_and_credentials(value):
    with pytest.raises(ConversationFailure):encode(value)


def test_codec_supports_only_safe_interrupt_and_json_roundtrip():
    value={'values':[1,False,None,{'text':'fiktif'}],'interrupt':Interrupt({'question':'OK?'},id='fictional')}
    result=decode(encode(value));assert result['values']==value['values'] and result['interrupt'].value=={'question':'OK?'}
    assert result['interrupt'].id=='fictional'


def saver():
    return BoundCheckpointSaver(AsyncMock(),credential_hash='a'*64,tenant_id=str(uuid4()),run_id=str(uuid4()),job_id=str(uuid4()),generation=1,epoch=1)


@pytest.mark.parametrize('config',[None,{}, {'configurable':{}},{'configurable':{'thread_id':str(uuid4())}}])
def test_thread_cannot_override_binding(config):
    with pytest.raises(ConversationFailure):saver().payload(config)


def test_no_global_history_or_delete_or_sync_bypass():
    s=saver()
    async def run():
        with pytest.raises(ConversationFailure):[v async for v in s.alist(None)]
        with pytest.raises(NotImplementedError):await s.adelete_thread(s.binding['run_id'])
    asyncio.run(run())
    with pytest.raises(NotImplementedError):s.get_tuple(s.config())
    assert s.repository.call.await_count==0


@pytest.mark.parametrize('decisions',[(),[True],(1,), (False,)])
def test_guard_rejection_rolls_back_before_write(decisions):
    events=[]
    @asynccontextmanager
    async def tx():
        events.append('begin')
        try:yield object()
        except BaseException:events.append('rollback');raise
        else:events.append('commit')
    @asynccontextmanager
    async def hold(subject,refs):
        events.append('enter')
        try:yield decisions
        finally:events.append('exit')
    ref=dict(system='service',kind='claim',resource_id=str(uuid4()),version='1',relation='owner',observed_revision='1')
    repo=PostgresCheckpointRepository(SimpleNamespace(transaction=tx),SimpleNamespace(hold=hold))
    repo.execute=AsyncMock(return_value=dict(tenant_id=str(uuid4()),principal_id=str(uuid4()),sources=[ref]))
    with pytest.raises(ConversationFailure):asyncio.run(repo.call('put',{}))
    assert events==['begin','enter','exit','rollback'] and repo.execute.await_count==1


def test_source_pin_covers_final_fence_and_commit_and_post_exit_deadline():
    events=[]
    @asynccontextmanager
    async def tx():
        events.append('begin');yield object();events.append('commit')
    @asynccontextmanager
    async def hold(subject,refs):
        events.append('enter');yield ();events.append('exit')
    repo=PostgresCheckpointRepository(SimpleNamespace(transaction=tx),SimpleNamespace(hold=hold))
    repo.execute=AsyncMock(side_effect=[dict(tenant_id=str(uuid4()),principal_id=str(uuid4()),sources=[]),dict(authority_expires_at=0)])
    with pytest.raises(ConversationFailure) as error:asyncio.run(repo.call('get',{}))
    assert error.value.status==401 and events==['begin','enter','commit','exit']


def test_cancellation_releases_source_and_rolls_back():
    events=[]
    @asynccontextmanager
    async def tx():
        try:yield object()
        except BaseException:events.append('rollback');raise
    @asynccontextmanager
    async def hold(subject,refs):
        try:yield ()
        finally:events.append('source_exit')
    repo=PostgresCheckpointRepository(SimpleNamespace(transaction=tx),SimpleNamespace(hold=hold))
    async def execute(connection,op,payload):
        if op=='stage':return dict(tenant_id=str(uuid4()),principal_id=str(uuid4()),sources=[])
        raise asyncio.CancelledError()
    repo.execute=execute
    with pytest.raises(asyncio.CancelledError):asyncio.run(repo.call('put',{}))
    assert events==['source_exit','rollback']


def test_run_validation_and_method_errors_have_private_headers():
    import httpx
    from enterprise_ai_platform.main import create_app
    from enterprise_ai_platform.config import Settings
    from enterprise_ai_platform.composition import ApplicationServices
    async def run():
        app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=SimpleNamespace(),conversations=SimpleNamespace()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://fixture.test') as client:
            path='/api/v1/runs/'+str(uuid4())
            for response in (await client.post(path+'/resume',json={'expected_version':True}),await client.delete(path)):
                assert response.status_code in (422,405)
                assert response.headers['cache-control']=='no-store' and response.headers['referrer-policy']=='no-referrer'
    asyncio.run(run())


def test_async_pending_write_waits_for_parent_put_outside_repository():
    async def run():
        s=saver();checkpoint_id=str(uuid4());events=[]
        async def call(operation,payload):
            events.append(operation)
            return dict(items=[])
        s.repository.call.side_effect=call
        task=asyncio.create_task(s.aput_writes(s.config(checkpoint_id=checkpoint_id),[('value',1)],'task'))
        await asyncio.sleep(0);assert not task.done() and events==['get']
        await s.aput(s.config(),dict(id=checkpoint_id,channel_values={'value':0}),{'step':0},{})
        await task;assert events==['get','put','writes']
    asyncio.run(run())
