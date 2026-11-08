import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from enterprise_ai_platform.infrastructure.database.document_sources import DatabaseDocumentSources
from enterprise_ai_platform.modules.conversations.ports import SourceSubject,ConversationFailure

def source_fixture(decisions,*,subject_change=False,deadline=10**12):
    subject=SourceSubject(str(uuid4()),str(uuid4()))
    row=dict(status='ok',tenant_id=subject.tenant_id,principal_id=str(uuid4()) if subject_change else subject.principal_id,decisions=decisions,authority_expires_at=deadline)
    connection=SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(fetchone=AsyncMock(return_value=(row,)))))
    guard=DatabaseDocumentSources(SimpleNamespace(schema='platform'),connection,dict(token='hashed-session'),history=True)
    return subject,guard,connection

@pytest.mark.parametrize('decisions',[[1],None,[True],[]])
def test_live_guard_rejects_wrong_decision_shape(decisions):
    subject,guard,connection=source_fixture(decisions)
    async def run():
        if decisions==[]:
            async with guard.hold(subject,()) as value:assert value==()
        else:
            with pytest.raises(ConversationFailure):
                async with guard.hold(subject,()):raise AssertionError('must not yield')
    asyncio.run(run());connection.execute.assert_awaited_once()

@pytest.mark.parametrize('change',[dict(subject_change=True),dict(deadline=0),dict(deadline=float('nan')),dict(deadline=True)])
def test_live_guard_identity_and_post_commit_expiry_are_not_trusted(change):
    subject,guard,_=source_fixture([],**change)
    async def run():
        with pytest.raises(ConversationFailure):
            async with guard.hold(subject,()):raise AssertionError('must not yield')
    asyncio.run(run())

def test_diagnostics_recovery_routes_have_owner_fields_and_private_errors():
    import httpx
    from enterprise_ai_platform.main import create_app
    from enterprise_ai_platform.config import Settings
    from enterprise_ai_platform.composition import ApplicationServices
    rid=str(uuid4());service=SimpleNamespace(diagnostics=AsyncMock(return_value=dict(id=rid)),recover=AsyncMock(return_value=dict(state='failed')))
    async def run():
        app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=SimpleNamespace(),conversations=service))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://fixture.test') as client:
            path='/api/v1/runs/'+rid
            assert (await client.get(path+'/diagnostics')).status_code==200
            assert (await client.post(path+'/recover',json=dict(expected_version=3))).status_code==200
            for body in ({'expected_version':None},{'expected_version':True},{'expected_version':3,'tenant_id':rid}):
                response=await client.post(path+'/recover',json=body)
                assert response.status_code==422 and response.headers['cache-control']=='no-store'
    asyncio.run(run());assert service.recover.await_count==1

def test_repeated_cancel_waits_for_sdk_completion_and_records_known_usage_once():
    from threading import Event
    from enterprise_ai_platform.modules.rag.service import RagTurn
    from enterprise_ai_platform.modules.models.ports import Usage
    entered=Event();release=Event();adapter=SimpleNamespace(last_usage=None)
    async def run():
        repo=SimpleNamespace(call=AsyncMock(return_value=dict(authority_expires_at=10**12,settlement_receipt='a'*64)),settle=AsyncMock())
        turn=RagTurn(repo,None,None,None,None);turn.question='fixture';turn.ctx=SimpleNamespace(prompt='fixture',hits=())
        observer=turn.observer(asyncio.get_running_loop())
        def provider():
            observer('chat',4096);entered.set();assert release.wait(2)
            adapter.last_usage=Usage(11,5);return 'done'
        task=asyncio.create_task(turn.provider('chat',adapter,provider))
        try:
            for _ in range(100):
                if entered.is_set():break
                await asyncio.sleep(.005)
            assert entered.is_set();task.cancel();await asyncio.sleep(0);task.cancel();await asyncio.sleep(.01)
            assert not task.done() and repo.settle.await_count==0
            release.set()
            with pytest.raises(asyncio.CancelledError):await task
            assert repo.settle.await_count==1 and repo.settle.call_args.args[2]==Usage(11,5)
        finally:release.set();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())
