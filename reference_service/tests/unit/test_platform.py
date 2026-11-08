import asyncio
import copy
import json
import secrets
from uuid import uuid4
import httpx
import pytest
from reimbursement_service.integrations.platform.client import PlatformClient,PlatformTarget
from reimbursement_service.modules.access.domain import AccessFailure,FailureKind
from reimbursement_service.modules.access.service import AccessService,Dispatcher,credential_digest
from reimbursement_service.modules.access.domain import request_hash

NOW=2000000000


def payload():
    return dict(schema_version=1,action='trip.read',tenant_id=str(uuid4()),actor_id=str(uuid4()),
                operation_id=str(uuid4()),object=dict(kind='trip',id=str(uuid4()),version=1),data={})


def response(p):
    return dict(allow=True,scope='actor_only',actor_id=p['actor_id'],tenant_id=p['tenant_id'],
        membership_id=str(uuid4()),access_revision=1,action=p['action'],operation_id=p['operation_id'],
        request_hash=request_hash(p),verified_at=NOW,expires_at=NOW+30,replayed=False)


class Stream(httpx.AsyncByteStream):
    def __init__(self,content):self.content=content
    async def __aiter__(self):yield self.content


def transport(body,status=200,headers=None,seen=None):
    def handle(request):
        if seen is not None:seen.append(request)
        return httpx.Response(status,headers=headers or {'content-type':'application/json'},
                              stream=Stream(body if isinstance(body,bytes) else json.dumps(body).encode()))
    return httpx.MockTransport(handle)


def client(body,**kwargs):
    return PlatformClient(PlatformTarget('https://platform.test',secrets.token_urlsafe(32)),
                          transport=transport(body,**kwargs),clock=lambda:NOW)


def test_exact_wire_and_no_actor_cache():
    async def run():
        p=payload(); seen=[]; c=client(response(p),seen=seen)
        try:
            assert (await c.verify('signed.fixture',p)).scope=='actor_only'
            await c.verify('signed.fixture',p)
            assert len(seen)==2 and seen[0].url.path=='/internal/v1/actors/verify'
            assert json.loads(seen[0].content)=={'payload':p}
            assert seen[0].headers['x-eai-delegation']=='signed.fixture'
        finally:await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('changes',[{'allow':False},{'scope':'owner'},{'actor_id':str(uuid4())},
    {'request_hash':'0'*64},{'access_revision':True},{'replayed':1},{'expires_at':NOW},
    {'expires_at':NOW+61},{'verified_at':NOW+6},{'extra':True},{'action':'claim.read'}])
def test_malformed_or_misbound_allow_denied(changes):
    async def run():
        p=payload(); c=client({**response(p),**changes})
        try:
            with pytest.raises(AccessFailure) as caught:await c.verify('token',p)
            assert caught.value.kind is FailureKind.PROTOCOL
        finally:await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('status,kind',[(400,FailureKind.INVALID),(401,FailureKind.UNAUTHENTICATED),
    (403,FailureKind.FORBIDDEN),(409,FailureKind.CONFLICT),(503,FailureKind.UNAVAILABLE),
    (429,FailureKind.UNAVAILABLE),(302,FailureKind.PROTOCOL),(404,FailureKind.PROTOCOL)])
def test_typed_errors_ignore_remote_body_and_never_retry(status,kind):
    async def run():
        seen=[];c=client(b'credential=private remote traceback',status=status,seen=seen)
        try:
            with pytest.raises(AccessFailure) as caught:await c.verify('token',payload())
            assert caught.value.kind is kind and 'private' not in str(caught.value)
            assert len(seen)==1
        finally:await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('body,headers',[(b'{"allow":true,"allow":true}',None),
    (b'{"x":NaN}',None),(b'\xff',None),(b'x'*65537,None),(b'{}',{'content-type':'text/html'}),
    (b'{}',{'content-type':'application/json','content-encoding':'gzip'})])
def test_transport_success_is_not_trusted(body,headers):
    async def run():
        c=client(body,headers=headers)
        try:
            with pytest.raises(AccessFailure) as caught:await c.verify('token',payload())
            assert caught.value.kind is FailureKind.PROTOCOL
        finally:await c.close()
    asyncio.run(run())


def test_network_fault_and_cancellation():
    async def run():
        async def fault(request):raise httpx.ReadTimeout('secret address')
        c=PlatformClient(PlatformTarget('https://p.test',secrets.token_urlsafe(32)),transport=httpx.MockTransport(fault))
        try:
            with pytest.raises(AccessFailure) as caught:await c.verify('token',payload())
            assert caught.value.kind is FailureKind.UNAVAILABLE and 'secret' not in str(caught.value)
        finally:await c.close()
    asyncio.run(run())


def test_ingress_dispatcher_is_separate_and_route_bound():
    async def run():
        p=payload();seen=[];c=client(response(p),seen=seen)
        ingress=secrets.token_urlsafe(32)
        service=AccessService(c,Dispatcher('platform-dispatcher',credential_digest(ingress),frozenset({'trip.read'})))
        headers=[(b'authorization',('Bearer '+ingress).encode()),(b'x-eai-delegation',b'token'),(b'content-type',b'application/json')]
        expected=dict(action='trip.read',object_kind='trip',object_id=p['object']['id'],object_version=1)
        try:
            assert (await service.authorize(headers,p,**expected)).scope=='actor_only'
            assert seen[0].headers['authorization']!='Bearer '+ingress
            for bad in [headers+headers[:1],headers+[(b'cookie',b'session=x')],headers[1:],
                        [(b'authorization',('Bearer '+secrets.token_urlsafe(32)).encode())]+headers[1:]]:
                before=len(seen)
                with pytest.raises(AccessFailure) as caught:await service.authorize(bad,p,**expected)
                assert caught.value.kind is FailureKind.UNAUTHENTICATED and len(seen)==before
            with pytest.raises(AccessFailure):await service.authorize(headers,p,**{**expected,'object_version':2})
        finally:await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('origin',['http://platform.test','https://p.test/path','https://x:y@p.test','https://p.test?x=1'])
def test_config_no_untrusted_endpoint(origin):
    with pytest.raises(ValueError):PlatformTarget(origin,secrets.token_urlsafe(32))


@pytest.mark.parametrize('moment,allowed',[(NOW+29,True),(NOW+30,False),(NOW+31,False)])
def test_ingress_final_expiry_boundary(moment,allowed):
    async def run():
        p=payload();c=client(response(p));secret=secrets.token_urlsafe(32)
        service=AccessService(c,Dispatcher('platform-dispatcher',credential_digest(secret),frozenset({'trip.read'})),clock=lambda:moment)
        headers=[(b'authorization',('Bearer '+secret).encode()),(b'x-eai-delegation',b'token'),(b'content-type',b'application/json')]
        expected=dict(action='trip.read',object_kind='trip',object_id=p['object']['id'],object_version=1)
        try:
            if allowed:assert (await service.authorize(headers,p,**expected)).scope=='actor_only'
            else:
                with pytest.raises(AccessFailure) as caught:await service.authorize(headers,p,**expected)
                assert caught.value.kind is FailureKind.UNAUTHENTICATED
        finally:await c.close()
    asyncio.run(run())


def test_clock_advance_during_final_hash_denies(monkeypatch):
    from reimbursement_service.modules.access import service as module
    moment=[NOW]
    original=module.request_hash
    def delayed_hash(p):
        result=original(p);moment[0]=NOW+30;return result
    monkeypatch.setattr(module,'request_hash',delayed_hash)
    async def run():
        p=payload();c=client(response(p));secret=secrets.token_urlsafe(32)
        service=AccessService(c,Dispatcher('platform-dispatcher',credential_digest(secret),frozenset({'trip.read'})),clock=lambda:moment[0])
        try:
            with pytest.raises(AccessFailure) as caught:
                await service.authorize([(b'authorization',('Bearer '+secret).encode()),(b'x-eai-delegation',b'token'),
                    (b'content-type',b'application/json')],p,action='trip.read',object_kind='trip',object_id=p['object']['id'],object_version=1)
            assert caught.value.kind is FailureKind.UNAUTHENTICATED
        finally:await c.close()
    asyncio.run(run())
