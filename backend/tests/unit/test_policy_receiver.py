import asyncio
import base64
import json
import hashlib
import hmac
import httpx
import pytest
from enterprise_ai_platform.delegation_check import SigningFixture,ISSUER,AUDIENCE,CALLER
from enterprise_ai_platform.integrations.security.catalog import RSACatalogSigner
from enterprise_ai_platform.integrations.security.delegation import RSADelegationVerifier
from enterprise_ai_platform.integrations.reimbursement.policies import PolicyReceiver,PolicyTarget
from enterprise_ai_platform.modules.delegation.domain import catalog_hash
from enterprise_ai_platform.modules.policies.domain import PolicyFailure
from test_policies import body,uid
from reimbursement_service.modules.policies.domain import canonical_body,request_digest

NOW=1800000000
CONTEXT={'tenant_id':uid(1),'actor_id':uid(7),'membership_id':uid(9),'session_id':uid(10),'access_revision':2,'authority_expires_at':NOW+30,'expected_version':2}

def ack():return {'package_id':uid(2),'package_version':1,'digest':body()['digest'],'state':'received','received_at':NOW}

def client(handler,clock=lambda:NOW):
    key=SigningFixture();signer=RSACatalogSigner(key.private,'current',ISSUER,AUDIENCE,CALLER)
    value=PolicyReceiver(PolicyTarget('https://reference.example.test','r'*43),signer,transport=httpx.MockTransport(handler),clock=clock)
    return value,key

def test_real_v2_signature_and_independent_receiver_binding_preserve_intent():
    calls=[];holder={}
    def handle(request):
        assert request.method=='PUT' and request.url.path==f'/service/v1/policy-packages/{uid(2)}/versions/1'
        assert request.headers['authorization']=='Bearer '+'r'*43 and 'cookie' not in request.headers
        raw=request.headers['x-eai-catalog-payload'];payload=json.loads(base64.urlsafe_b64decode(raw+'='*((-len(raw))%4)))
        claims=RSADelegationVerifier().verify(request.headers['x-eai-delegation'],holder['key'].jwk)
        assert claims['canonical_version']==2 and claims['human_receipt_id'] is None
        assert claims['sub']==CONTEXT['actor_id'] and claims['session_id']==CONTEXT['session_id']
        assert claims['request_hash']==catalog_hash(payload) and claims['exp']==NOW+30
        assert payload['data']['request_digest']==request_digest(uid(2),1,canonical_body(json.loads(request.content)))
        assert request.headers['idempotency-key']==payload['data']['key']==uid(6)
        assert request.headers['x-eai-operation-id']==payload['operation_id']==claims['operation_id']==uid(8)
        calls.append((request.content,claims['jti']))
        return httpx.Response(200,json=ack())
    value,key=client(handle);holder['key']=key
    async def run():
        try:
            first=await value.receive(CONTEXT,body(),uid(2),1,uid(6),uid(8));second=await value.receive(CONTEXT,body(),uid(2),1,uid(6),uid(8))
            assert dict(first.ack)==dict(second.ack)==ack() and first.proof==second.proof and first.key_id=='reference-v1'
        finally:await value.close()
    asyncio.run(run())
    assert calls[0][0]==calls[1][0] and calls[0][1]!=calls[1][1]

@pytest.mark.parametrize('kind',['redirect','timeout','wrong_digest','duplicate_json','extra_content','oversize','content_type','expired_context'])
def test_upstream_faults_never_fabricate_ack_or_follow_redirect(kind):
    count=[]
    def handler(request):
        count.append(1)
        if kind=='redirect':return httpx.Response(307,headers={'location':'https://foreign.example.test/private'})
        if kind=='timeout':raise httpx.ReadTimeout('sensitive details')
        if kind=='duplicate_json':return httpx.Response(200,content=b'{"state":"received","state":"active"}',headers={'content-type':'application/json'})
        if kind=='oversize':return httpx.Response(200,content=b' '*8193,headers={'content-type':'application/json'})
        if kind=='content_type':return httpx.Response(200,content=b'{}',headers={'content-type':'text/html'})
        result=ack()
        if kind=='wrong_digest':result['digest']='b'*64
        if kind=='extra_content':result['document_text']='sensitive details'
        return httpx.Response(200,json=result)
    value,_=client(handler);context={**CONTEXT,'authority_expires_at':NOW} if kind=='expired_context' else CONTEXT
    async def run():
        try:
            with pytest.raises(PolicyFailure) as error:await value.receive(context,body(),uid(2),1,uid(6),uid(8))
            assert error.value.status==503 and 'sensitive' not in str(error.value)
        finally:await value.close()
    asyncio.run(run());assert len(count)==(0 if kind=='expired_context' else 1)

@pytest.mark.parametrize('origin',['http://reference.example.test','https://user:secret@reference.example.test','https://reference.example.test/path','https://reference.example.test?query=1'])
def test_target_cannot_include_insecure_or_user_controlled_path(origin):
    with pytest.raises(ValueError):PolicyTarget(origin,'r'*43)

def test_target_repr_does_not_expose_dispatcher_credential():
    assert 'r'*43 not in repr(PolicyTarget('https://reference.example.test','r'*43))


def test_settlement_proof_binds_actor_package_intent_version_and_exact_ack():
    value,_=client(lambda request:httpx.Response(200,json=ack()))
    async def run():
        try:
            received=await value.receive(CONTEXT,body(),uid(2),1,uid(6),uid(8))
            # Construct independently of the production settlement_material helper.
            material=dict(tenant_id=uid(1),actor_id=uid(7),package_id=uid(2),package_version=1,
                          key=uid(6),operation_id=uid(8),expected_version=2,ack=ack())
            secret=hashlib.sha256(b'eai:policy-settlement:key:v1:'+b'r'*43).digest()
            def sign(item):
                raw=json.dumps(item,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
                return hmac.new(secret,raw,hashlib.sha256).hexdigest()
            assert received.proof==sign(material)
            for field,new in [('tenant_id',uid(90)),('actor_id',uid(91)),('package_id',uid(92)),
                              ('package_version',2),('key',uid(93)),('operation_id',uid(94)),('expected_version',3),
                              ('ack',{**ack(),'received_at':NOW-1})]:
                assert received.proof!=sign({**material,field:new})
            assert received.proof not in repr(received)
            with pytest.raises(TypeError):received.ack['state']='active'
        finally:await value.close()
    asyncio.run(run())
