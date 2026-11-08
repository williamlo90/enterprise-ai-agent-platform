"""G-13 boundary/transaction wiring. Real SQL and client races have separate evidence."""
import asyncio
from contextlib import asynccontextmanager
import time
import pytest
import httpx
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse, StreamingResponse
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.modules.identity.ports import BrowserReadScope, browser_read_scope, BrowserContextFailure
from enterprise_ai_platform.modules.identity.service import digest
from enterprise_ai_platform.infrastructure.database.knowledge import PostgresKnowledgeRepository
from enterprise_ai_platform.modules.knowledge.ports import KnowledgeFailure

BINDING='a'*64
TOKEN='t'*43
PATH='/api/v1/knowledge/probe'


def client(*,enabled=True,attest=True,expired=False,stream=False):
    settings=Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':enabled})
    app=create_app(settings)
    @app.get(PATH)
    async def probe():
        context=browser_read_scope.get()
        if attest and context:
            context.attestation={'context_binding':BINDING,'authority_expires_at':time.time()+(-1 if expired else 60)}
        if stream:
            async def data():yield b'private-fixture'
            return StreamingResponse(data())
        return JSONResponse({'value':'private-fixture'})
    return TestClient(app)


def headers(binding=BINDING):
    return {'Cookie':'__Host-eai-session='+TOKEN,'X-EAI-Expected-Context':binding}


def test_disabled_protocol_never_accepts_a_new_bound_client():
    response=client(enabled=False).get(PATH,headers=headers())
    assert response.status_code==503 and 'private-fixture' not in response.text


@pytest.mark.parametrize('binding',[None,'', 'a'*63,'g'*64])
def test_enabled_missing_or_malformed_expectation_never_dispatches_private_body(binding):
    values=headers()
    if binding is None:values.pop('X-EAI-Expected-Context')
    else:values['X-EAI-Expected-Context']=binding
    response=client().get(PATH,headers=values)
    assert response.status_code==400 and 'private-fixture' not in response.text


@pytest.mark.parametrize('options,status', [({'attest':False},503),({'expired':True},401),({'stream':True},503)])
def test_missing_final_attestation_expiry_and_streaming_are_fail_closed(options,status):
    response=client(**options).get(PATH,headers=headers())
    assert response.status_code==status and 'private-fixture' not in response.text
    assert 'x-eai-context-binding' not in response.headers


def test_response_binding_is_not_an_echo_of_client_input_and_private_cache_is_disabled():
    response=client().get(PATH,headers=headers('b'*64))
    assert response.status_code==409 and 'private-fixture' not in response.text
    response=client().get(PATH,headers=headers())
    assert response.status_code==200 and response.headers['x-eai-context-binding']==BINDING
    assert response.headers['cache-control']=='no-store'


def test_rollout_is_strict_boolean_and_requires_auth_database():
    with pytest.raises(ValueError):Settings(env='test',host='localhost',port=8000,browser_context_enabled=True)
    with pytest.raises(ValueError):Settings(env='test',host='localhost',port=8000,browser_context_enabled='yes')


class Connection:
    def __init__(self):self.events=[]
    async def execute(self,statement,args=None):
        query=statement.as_string()
        if 'browser_context_guard' in query:
            self.events.append('guard')
            self.row={'status':'ok','context_binding':BINDING,'authority_expires_at':time.time()+60}
        else:
            self.events.append('read');self.row={'status':'ok','items':[]}
        return self
    async def fetchone(self):return (self.row,)


class Database:
    schema='platform'
    def __init__(self,fail=False):self.connection=Connection();self.fail=fail
    @asynccontextmanager
    async def transaction(self):
        yield self.connection
        self.connection.events.append('commit')
        if self.fail:raise RuntimeError('controlled commit failure')


@pytest.mark.parametrize('commit_failure',[False,True])
def test_guard_and_read_use_one_connection_and_attest_only_after_commit(commit_failure):
    async def run():
        token=digest('session',TOKEN)
        context=BrowserReadScope('read',token,expected=BINDING,family='knowledge',final_operation='search_recheck')
        marker=browser_read_scope.set(context)
        database=Database(fail=commit_failure)
        try:
            repo=PostgresKnowledgeRepository(database)
            if commit_failure:
                with pytest.raises(KnowledgeFailure):await repo.call(token,'search_recheck',{})
                assert context.attestation is None
            else:
                await repo.call(token,'search_recheck',{})
                assert context.response_binding()==BINDING
            assert database.connection.events==['guard','read','guard','commit']
        finally:browser_read_scope.reset(marker)
    asyncio.run(run())


def test_child_task_cannot_reuse_request_attestation_or_mint_a_read():
    async def run():
        token=digest('session',TOKEN)
        context=BrowserReadScope('read',token,expected=BINDING,family='knowledge',final_operation='search_recheck')
        marker=browser_read_scope.set(context)
        database=Database()
        try:
            with pytest.raises(BrowserContextFailure):
                await asyncio.create_task(PostgresKnowledgeRepository(database).call(token,'search_recheck',{}))
            assert not database.connection.events
        finally:browser_read_scope.reset(marker)
        assert browser_read_scope.get() is None
    asyncio.run(run())


def test_cancelled_request_deactivates_scope_and_cannot_lend_attestation_to_a_later_request():
    async def run():
        settings=Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True})
        app=create_app(settings);entered=asyncio.Event();scopes=[]
        @app.get(PATH)
        async def probe():
            context=browser_read_scope.get();scopes.append(context);entered.set()
            context.attestation={'context_binding':BINDING,'authority_expires_at':time.time()+60}
            await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://fixture.test') as session:
            request=asyncio.create_task(session.get(PATH,headers=headers()))
            await entered.wait();request.cancel()
            with pytest.raises(asyncio.CancelledError):await request
        assert scopes and not scopes[0].active and scopes[0].attestation is None
        assert browser_read_scope.get() is None
    asyncio.run(run())


@pytest.mark.parametrize('path,operation',[
 ('/api/v1/conversations/77777777-7777-4777-8777-777777777777/messages','append'),
 ('/api/v1/messages/77777777-7777-4777-8777-777777777777/feedback','feedback'),
 ('/api/v1/runs/77777777-7777-4777-8777-777777777777/recover','recover'),
])
@pytest.mark.parametrize('attest',[False,True])
def test_chat_mutations_require_bound_context_and_committed_attestation(path,operation,attest):
    settings=Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True})
    app=create_app(settings);seen=[]
    # Replace only the endpoint with a controlled probe; real service/SQL is
    # checked separately by the restricted database/browser journey.
    app.router.routes=[r for r in app.router.routes if not (getattr(r,'path',None) in ('/api/v1/conversations/{conversation_id}/messages','/api/v1/messages/{message_id}/feedback','/api/v1/runs/{run_id}/recover'))]
    @app.post(path)
    async def probe():
        context=browser_read_scope.get();seen.append(context)
        assert context.family=='conversations' and context.final_operation==operation
        if attest:context.attestation={'context_binding':BINDING,'authority_expires_at':time.time()+60}
        return JSONResponse({'accepted':True})
    with TestClient(app) as c:
        assert c.post(path,headers={'Cookie':'__Host-eai-session='+TOKEN}).status_code==400
        assert not seen
        response=c.post(path,headers=headers())
        assert response.status_code==(200 if attest else 503)
        assert response.headers.get('x-eai-context-binding')==(BINDING if attest else None)
