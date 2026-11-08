import asyncio
import copy
import json
from types import SimpleNamespace
from uuid import UUID
import httpx
import pytest
from enterprise_ai_platform.integrations.reimbursement.policies import PolicyTarget
from enterprise_ai_platform.integrations.reimbursement.receipt_access import ClaimReceiptReader
from enterprise_ai_platform.modules.delegation.domain import request_hash
from enterprise_ai_platform.modules.receipts.domain import ReceiptFailure


def uid(n):return str(UUID(int=n))


class Signer:
    issuer='fixture';audience='reference-reimbursement';caller='platform-dispatcher'
    def __init__(self):self.claims=None
    def sign(self,claims,*,now):self.claims=copy.deepcopy(claims);return 'controlled-signed-delegation'


def fixture(*,mutate=None,status=200,raw=None,delay=None):
    now=[1000];context=dict(tenant_id=uid(1),actor_id=uid(2),membership_id=uid(3),session_id=uid(4),access_revision=7,authority_expires_at=1050)
    requests=[];signer=Signer()
    def handler(request):
        requests.append(request);payload=json.loads(request.content)['payload']
        assert request.url.path=='/service/v1/claims/'+uid(5)+'/access-check'
        assert request.headers['authorization']=='Bearer '+'a'*43
        assert request.headers['x-eai-delegation']=='controlled-signed-delegation'
        assert payload['action']=='claim.read' and payload['object']==dict(kind='claim',id=uid(5),version=1)
        assert signer.claims['request_hash']==request_hash(payload) and signer.claims['human_receipt_id'] is None
        body=dict(allow=True,tenant_id=uid(1),actor_id=uid(2),claim_id=uid(5),claim_version=3,revision=2,
            receipts=[dict(id=uid(6),version=2,digest='a'*64)],operation_id=payload['operation_id'],request_hash=request_hash(payload),checked_at=1000,expires_at=1040)
        if mutate:mutate(body)
        if delay:delay(now,context)
        if raw is not None:return httpx.Response(status,content=raw,headers={'content-type':'application/json'})
        return httpx.Response(status,json=body)
    reader=ClaimReceiptReader(PolicyTarget('https://fixture.test','a'*43),signer,transport=httpx.MockTransport(handler),clock=lambda:now[0])
    async def access():
        try:return await reader.access(context,uid(5))
        finally:await reader.close()
    return SimpleNamespace(now=now,context=context,requests=requests,signer=signer,reader=reader,access=access)


def test_exact_current_relation_is_typed_and_request_is_source_context_bound():
    c=fixture();result=asyncio.run(c.access())
    assert result.claim_version==3 and result.revision==2 and result.expires_at==1040
    assert c.signer.claims['exp']==1050 and c.signer.claims['access_revision']==7
    assert len(c.requests)==1


@pytest.mark.parametrize('change',[
    lambda v:v.update(allow=False),lambda v:v.update(allow=1),lambda v:v.update(tenant_id=uid(20)),
    lambda v:v.update(actor_id=uid(20)),lambda v:v.update(claim_id=uid(20)),lambda v:v.update(operation_id=uid(20)),
    lambda v:v.update(request_hash='b'*64),lambda v:v.update(checked_at=1006),lambda v:v.update(checked_at=True),
    lambda v:v.update(expires_at=1000),lambda v:v.update(expires_at=1051),lambda v:v.update(claim_version=True),
    lambda v:v.update(revision=0),lambda v:v.update(extra=True),lambda v:v['receipts'][0].update(digest='bad'),
    lambda v:v['receipts'].append(copy.deepcopy(v['receipts'][0])),
])
def test_unbound_stale_or_malformed_source_response_is_denied(change):
    c=fixture(mutate=change)
    with pytest.raises(ReceiptFailure) as error:asyncio.run(c.access())
    assert error.value.status==503 and len(c.requests)==1


@pytest.mark.parametrize('status,expected',[(302,503),(401,403),(403,403),(404,404),(409,409),(429,503),(500,503)])
def test_source_failure_never_redirects_or_retries(status,expected):
    c=fixture(status=status)
    with pytest.raises(ReceiptFailure) as error:asyncio.run(c.access())
    assert error.value.status==expected and len(c.requests)==1


@pytest.mark.parametrize('raw',[b'{"allow":true,"allow":true}',b'NaN',b'\xff',b' '*8193])
def test_duplicate_nonfinite_invalid_encoding_or_oversized_json_fails_closed(raw):
    c=fixture(raw=raw)
    with pytest.raises(ReceiptFailure):asyncio.run(c.access())


def test_authority_expiring_during_http_never_returns_relation():
    c=fixture(delay=lambda now,context:now.__setitem__(0,1050))
    with pytest.raises(ReceiptFailure):asyncio.run(c.access())


def test_context_mutation_during_http_does_not_replace_original_identity():
    c=fixture(delay=lambda now,context:context.update(actor_id=uid(20),tenant_id=uid(20)))
    result=asyncio.run(c.access())
    assert (result.actor_id,result.tenant_id)==(uid(2),uid(1))


@pytest.mark.parametrize('field,value',[('access_revision',True),('authority_expires_at',float('inf')),('authority_expires_at',999),('actor_id',uid(0))])
def test_invalid_private_context_never_makes_network_request(field,value):
    c=fixture();c.context[field]=value
    with pytest.raises(ReceiptFailure):asyncio.run(c.access())
    assert not c.requests
