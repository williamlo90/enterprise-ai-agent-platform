import asyncio
import base64
import copy
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock
import time
from uuid import UUID
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from enterprise_ai_platform.modules.receipts.domain import PinBatch,PinIdentity,ReceiptFailure,encoded
from enterprise_ai_platform.modules.receipts.service import ReceiptPinService
from enterprise_ai_platform.integrations.security.receipt_terminal import ReceiptTerminalVerifier,TerminalKey
from enterprise_ai_platform.integrations.security.receipt_settlement import ReceiptSettlement,SettlementKey
from enterprise_ai_platform.composition import ApplicationServices,receipt_service
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.internal_api import create_internal_app
from enterprise_ai_platform.api.receipt_boundary import ReceiptResponseBoundary


def uid(n):return str(UUID(int=n))


@pytest.fixture
def case():
    now=[int(time.time())];issued=now[0];clock=lambda:now[0]
    batch=PinBatch(PinIdentity(uid(1),uid(2),uid(3),'platform-dispatcher','claim.submit',uid(4),'a'*64,'draft',uid(5),3),uid(6),())
    private=Ed25519PrivateKey.generate()
    verifier=ReceiptTerminalVerifier({'test':TerminalKey(private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),issued-60,issued+200)},clock=clock)
    settlement=ReceiptSettlement(SettlementKey('fixture',b'k'*32,issued-60,issued+200),clock=clock)
    def proof(state='committed'):
        result=dict(ref=batch.claim_id,version=1,code='claim_submitted') if state=='committed' else dict(ref=None,version=None,code='sealed_no_commit')
        terminal=dict(identity=batch.identity.wire(),state=state,result=result,result_digest=sha256(b'reimbursement:operation-result:v1:'+encoded(result)).hexdigest(),completed_at=issued,ledger_revision=1)
        body=dict(schema_version=1,issuer='reference-reimbursement',audience='enterprise-ai-platform',kid='test',operation=terminal,issued_at=issued,expires_at=issued+40)
        return dict(payload=body,signature=base64.urlsafe_b64encode(private.sign(b'reimbursement:terminal-proof:v1:'+encoded(body))).decode().rstrip('='))
    row=dict(batch=batch.wire(),state='held',terminal_digest=None,claim_version=None,expires_at=issued+50)
    async def lookup(*args):return copy.deepcopy(row)
    async def finalise(credential,envelope):
        assert credential==sha256(('eai:internal-caller:v1:'+'a'*43).encode()).hexdigest()
        terminal=envelope['material']['terminal']
        result=dict(state='finalised' if terminal['state']=='committed' else 'released',terminal_digest=terminal['terminal_digest'],claim_version=terminal['result_version'],expires_at=terminal['expires_at'])
        row.update({k:v for k,v in result.items() if k!='expires_at'})
        return result
    repo=SimpleNamespace(lookup=AsyncMock(side_effect=lookup),finalise=AsyncMock(side_effect=finalise))
    service=ReceiptPinService(repo,proofs=verifier,settlement=settlement,clock=clock)
    values=dict(tenant_id=uid(1),proof=proof())
    return SimpleNamespace(now=now,issued=issued,clock=clock,batch=batch,repo=repo,row=row,service=service,values=values,proof=proof,finalise=finalise,verifier=verifier,settlement=settlement)


@pytest.mark.parametrize('state',['committed','sealed_no_commit'])
def test_actual_crypto_mac_chain_uses_durable_batch_and_exact_retry(case,state):
    case.values['proof']=case.proof(state)
    first=asyncio.run(case.service.finalise('a'*43,uid(2),case.values))
    assert first['state']==('finalised' if state=='committed' else 'released')
    assert asyncio.run(case.service.finalise('a'*43,uid(2),case.values))==first
    assert case.repo.finalise.call_count==2


def test_default_proof_or_mac_dependency_does_not_touch_registry(case):
    for kwargs in ({},{'proofs':case.verifier},{'settlement':case.settlement}):
        with pytest.raises(ReceiptFailure) as error:asyncio.run(ReceiptPinService(case.repo,**kwargs).finalise('a'*43,uid(2),case.values))
        assert error.value.status==503
    case.repo.lookup.assert_not_called();case.repo.finalise.assert_not_called()


@pytest.mark.parametrize('change',[
    lambda c:c.values.update(extra=True),lambda c:c.values.update(tenant_id=uid(0)),
    lambda c:c.values.update(proof='x'*34000),lambda c:c.values.update(proof=float('nan')),
])
def test_invalid_material_is_rejected_before_lookup(case,change):
    change(case)
    with pytest.raises(ReceiptFailure) as error:asyncio.run(case.service.finalise('a'*43,uid(2),case.values))
    assert error.value.status==400;case.repo.lookup.assert_not_called()


def test_forged_terminal_proof_cannot_call_settlement_repository(case):
    case.values['proof']['signature']='A'*86
    with pytest.raises(ReceiptFailure) as error:asyncio.run(case.service.finalise('a'*43,uid(2),case.values))
    assert error.value.status==403;case.repo.finalise.assert_not_called()


def test_finalised_batch_cannot_be_released_by_conflicting_seal(case):
    asyncio.run(case.service.finalise('a'*43,uid(2),case.values));case.repo.finalise.reset_mock()
    case.values['proof']=case.proof('sealed_no_commit')
    with pytest.raises(ReceiptFailure) as error:asyncio.run(case.service.finalise('a'*43,uid(2),case.values))
    assert error.value.status==409;case.repo.finalise.assert_not_called()


@pytest.mark.parametrize('change',[
    lambda r:r['batch']['identity'].update(operation_id=uid(20)),lambda r:r.update(extra=True),
    lambda r:r.update(expires_at=True),lambda r:r.update(state='unknown'),
])
def test_wrong_or_malformed_durable_batch_never_settles(case,change):
    change(case.row)
    with pytest.raises(ReceiptFailure):asyncio.run(case.service.finalise('a'*43,uid(2),case.values))
    case.repo.finalise.assert_not_called()


def test_lookup_cannot_mutate_original_caller_proof_during_await(case):
    async def mutate(*args):
        case.values['proof']['signature']='A'*86
        return copy.deepcopy(case.row)
    case.repo.lookup.side_effect=mutate
    assert asyncio.run(case.service.finalise('a'*43,uid(2),case.values))['state']=='finalised'


def test_caller_expiry_clamps_verified_evidence_and_mac(case):
    case.row['expires_at']=case.issued+20
    result=asyncio.run(case.service.finalise('a'*43,uid(2),case.values))
    assert result['expires_at']==case.issued+20
    assert case.repo.finalise.call_args.args[1]['material']['terminal']['expires_at']==case.issued+20


@pytest.mark.parametrize('field,value',[('state','released'),('terminal_digest','c'*64),('claim_version',True),('claim_version',2)])
def test_settlement_response_cannot_substitute_outcome_or_version(case,field,value):
    async def altered(*args):return {**await case.finalise(*args),field:value}
    case.repo.finalise.side_effect=altered
    with pytest.raises(ReceiptFailure) as error:asyncio.run(case.service.finalise('a'*43,uid(2),case.values))
    assert error.value.status==503


def test_expired_during_final_repository_await_never_reports_success(case):
    async def late(*args):
        result=await case.finalise(*args);case.now[0]=case.issued+40;return result
    case.repo.finalise.side_effect=late
    with pytest.raises(ReceiptFailure):asyncio.run(case.service.finalise('a'*43,uid(2),case.values))


def test_private_api_actual_service_and_public_absence(case):
    services=ApplicationServices(health=None,receipts=case.service)
    internal=create_internal_app(Settings(env='test',host='127.0.0.1',port=8000),services=services)
    public=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=services)
    names=[m.cls.__name__ for m in public.user_middleware]
    assert names.index('ReceiptResponseBoundary')<names.index('BrowserContextBoundary')
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=internal),base_url='https://fixture.test') as client:
            path='/internal/v1/receipt-pins/'+uid(2)+'/finalise';headers={'authorization':'Bearer '+'a'*43,'content-type':'application/json'}
            good=await client.post(path,json=case.values,headers=headers)
            assert good.status_code==200 and good.json()['state']=='finalised' and good.headers['cache-control']=='no-store'
            case.repo.lookup.reset_mock()
            for url,body,h in [(path+'?role=admin',encoded(case.values),headers),(path,b'{"tenant_id":"x","tenant_id":"y"}',headers),
                    (path,encoded(case.values),{**headers,'cookie':'session=no'}),(path,encoded(case.values),{**headers,'x-eai-delegation':'unexpected'})]:
                response=await client.post(url,content=body,headers=h)
                assert response.status_code in {400,401} and response.headers['cache-control']=='no-store'
            duplicate=[('authorization','Bearer '+'a'*43),('authorization','Bearer '+'a'*43),('content-type','application/json')]
            assert (await client.post(path,json=case.values,headers=duplicate)).status_code==401
            case.repo.lookup.assert_not_called()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public),base_url='https://fixture.test') as client:
            assert (await client.post(path,json=case.values)).status_code==404
            assert (await client.post('/internal/v1/receipt-pins',json={})).status_code==404
    asyncio.run(run())


def test_explicit_composition_uses_same_db_and_allocates_no_resources():
    database=SimpleNamespace();service=receipt_service(database)
    assert service.repository.database is database and service.proofs is None and service.settlement is None and service.delegation is None


@pytest.mark.parametrize('expiry,after_headers,expected',[(100,False,503),(101,False,200),(101,True,None)])
def test_private_response_guard_holds_bytes_and_checks_after_header_await(expiry,after_headers,expected):
    now=[100];sent=[];scope={'type':'http','state':{'request_id':'fixture','receipt_authority_deadline':expiry}}
    async def inner(scope,receive,send):
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'private-buffer','more_body':False})
    async def send(message):
        sent.append(message)
        if after_headers and message['type']=='http.response.start':now[0]=101
    boundary=ReceiptResponseBoundary(inner,clock=lambda:now[0])
    if expected is None:
        with pytest.raises(RuntimeError):asyncio.run(boundary(scope,None,send))
        assert not any(m.get('body')==b'private-buffer' for m in sent)
    else:
        asyncio.run(boundary(scope,None,send));assert sent[0]['status']==expected
        assert (any(m.get('body')==b'private-buffer' for m in sent))==(expected==200)


def test_claim_file_guard_accepts_large_bounded_attachment_but_never_releases_after_header_expiry():
    data=b'private'*6000
    async def run(expire):
        now=[100];messages=[];scope=dict(type='http',path='/api/v1/files/id/claims/claim/content',state=dict(request_id='fixture',receipt_authority_deadline=101))
        async def app(scope,receive,send):
            await send(dict(type='http.response.start',status=200,headers=[]))
            await send(dict(type='http.response.body',body=data,more_body=False))
        async def send(message):
            messages.append(message)
            if expire and message['type']=='http.response.start':now[0]=101
        if expire:
            with pytest.raises(RuntimeError):await ReceiptResponseBoundary(app,clock=lambda:now[0])(scope,None,send)
            assert not any(m.get('body')==data for m in messages)
        else:
            await ReceiptResponseBoundary(app,clock=lambda:now[0])(scope,None,send)
            assert messages[-1]['body']==data
    asyncio.run(run(False));asyncio.run(run(True))
