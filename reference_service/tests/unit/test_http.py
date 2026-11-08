import asyncio
from uuid import UUID
import httpx
import pytest
from reimbursement_service.config import Settings
from reimbursement_service.main import create_app
from reimbursement_service.api.boundary import RequestBoundary
from reimbursement_service.modules.system.service import HealthService


class FakeDatabase:
    def __init__(self): self.available=True; self.opened=0; self.closed=0
    async def open(self): self.opened+=1
    async def close(self): self.closed+=1
    async def ping(self): return self.available


def app_for(db):
    return create_app(Settings(database_url='unused fixture'), resource_factory=lambda settings:(db,HealthService(db)))


def test_health_lifecycle_unavailable_and_no_business_routes():
    async def run():
        db=FakeDatabase(); app=app_for(db)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://service.test') as client:
                assert (await client.get('/health/live')).json()=={'status':'alive'}
                ready=await client.get('/health/ready',headers={'X-Request-ID':'attacker'})
                assert ready.status_code==200 and UUID(ready.headers['x-request-id'])
                assert ready.headers['cache-control']=='no-store'
                db.available=False
                response=await client.get('/health/ready')
                assert response.status_code==503 and response.json()['error']['retryable']
                assert response.json()['error']['request_id']==response.headers['x-request-id']
                assert (await client.get('/health/live')).status_code==200
                for path in ['/docs','/openapi.json','/service/v1/claims','/internal/v1/actors/verify','/api/v1/me']:
                    assert (await client.get(path)).status_code==404
                assert (await client.post('/health/live')).status_code==405
        assert db.opened==1 and db.closed==1
    asyncio.run(run())


def test_app_instances_do_not_share_health_state():
    async def run():
        one,two=FakeDatabase(),FakeDatabase(); one.available=False
        for database,expected in [(one,503),(two,200)]:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app_for(database)),base_url='https://s.test') as client:
                assert (await client.get('/health/ready')).status_code==expected
    asyncio.run(run())


async def invoke(headers, chunks, *, failing=False, timeout=False):
    called=[]; output=[]
    async def downstream(scope,receive,send):
        called.append(True)
        if failing: raise ValueError('credential=secret')
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'OK'})
    events=iter(chunks)
    async def receive():
        if timeout: await asyncio.sleep(1)
        return next(events)
    async def send(event): output.append(event)
    await RequestBoundary(downstream,max_body_bytes=4,body_timeout=.01)(
        {'type':'http','headers':headers},receive,send)
    return called,output


@pytest.mark.parametrize('headers,chunks,status',[
    ([(b'content-length',b'5')],[],413),
    ([(b'content-length',b'-1')],[],400),
    ([(b'content-length',b'0'),(b'content-length',b'0')],[],400),
    ([],[{'type':'http.request','body':b'12345'}],413),
    ([(b'content-length',b'3')],[{'type':'http.request','body':b'12'}],400),
])
def test_reject_input_before_route(headers,chunks,status):
    called,output=asyncio.run(invoke(headers,chunks))
    assert not called and output[0]['status']==status


def test_chunked_body_timeout_disconnect_and_safe_500():
    async def run():
        called,output=await invoke([], [{'type':'http.request','body':b'12','more_body':True},
                                        {'type':'http.request','body':b'34'}])
        assert called and output[0]['status']==200
        called,output=await invoke([],[],timeout=True)
        assert not called and output[0]['status']==408
        called,output=await invoke([],[{'type':'http.disconnect'}])
        assert not called and not output
        _,output=await invoke([],[{'type':'http.request'}],failing=True)
        assert output[0]['status']==500 and b'secret' not in output[1]['body']
    asyncio.run(run())
