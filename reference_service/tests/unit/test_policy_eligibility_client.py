import asyncio
import copy
import json
from uuid import uuid4
import httpx
import pytest
from test_platform import Stream
from reimbursement_service.integrations.platform.client import PlatformClient,PlatformTarget
from reimbursement_service.integrations.platform.policies import PolicyEligibilityClient
from reimbursement_service.modules.access.domain import AccessFailure,FailureKind,request_hash

NOW=2000000000


def reply(value):return httpx.Response(200,headers={'content-type':'application/json'},stream=Stream(json.dumps(value).encode()))


def request():
    policy=dict(id=str(uuid4()),version=1,digest='a'*64);claim=str(uuid4())
    payload=dict(schema_version=1,action='claim.decide',tenant_id=str(uuid4()),actor_id=str(uuid4()),operation_id=str(uuid4()),
        object=dict(kind='claim',id=claim,version=1),data=dict(revision=1,stage=1,decision='approve',reason=''))
    return payload,dict(policy=policy,operation_kind='decision',trip_date='2026-05-01',claim_ref=claim)


def response(payload,values):
    return dict(allow=True,**values,operation_id=payload['operation_id'],request_hash=request_hash(payload),
        eligibility_revision=1,checked_at=NOW,expires_at=NOW+30,blocked_reason=None)


def test_live_metadata_uses_original_delegation_and_exact_request_without_caching_or_content_access():
    async def run():
        payload,values=request();calls=[]
        def handle(request):
            calls.append(request)
            assert request.url.path=='/internal/v1/policy-packages/'+values['policy']['id']+'/eligibility'
            assert request.headers['x-eai-delegation']=='signed.fixture' and 'cookie' not in request.headers
            assert json.loads(request.content)==dict(payload=payload,**values)
            return reply(response(payload,values))
        client=PlatformClient(PlatformTarget('https://platform.test','c'*43),transport=httpx.MockTransport(handle),clock=lambda:NOW)
        try:
            adapter=PolicyEligibilityClient(client)
            first=await adapter.eligibility('signed.fixture',payload,**values)
            assert first.allow and first.policy==values['policy']
            with pytest.raises(TypeError):first.policy['version']=2
            await adapter.eligibility('signed.fixture',payload,**values)
            assert len(calls)==2
        finally:await client.close()
    asyncio.run(run())


@pytest.mark.parametrize('changes',[{'allow':1},{'document_text':'secret'},{'policy':{'id':str(uuid4()),'version':1,'digest':'a'*64}},
    {'operation_id':str(uuid4())},{'request_hash':'b'*64},{'operation_kind':'submission'},{'trip_date':'2026-05-02'},
    {'claim_ref':str(uuid4())},{'eligibility_revision':True},{'checked_at':NOW+6},{'expires_at':NOW},{'expires_at':NOW+61},
    {'blocked_reason':'withdrawn'},{'allow':False,'blocked_reason':None},{'allow':False,'blocked_reason':'secret arbitrary reason'}])
def test_malformed_extra_content_or_misbound_metadata_fails_closed(changes):
    async def run():
        payload,values=request();result={**response(payload,values),**changes}
        client=PlatformClient(PlatformTarget('https://platform.test','c'*43),transport=httpx.MockTransport(lambda _:reply(result)),clock=lambda:NOW)
        try:
            with pytest.raises(AccessFailure) as error:await PolicyEligibilityClient(client).eligibility('signed.fixture',payload,**values)
            assert error.value.kind is FailureKind.PROTOCOL
        finally:await client.close()
    asyncio.run(run())


def test_denied_metadata_is_a_hold_and_is_not_converted_into_business_rejection():
    async def run():
        payload,values=request();result={**response(payload,values),'allow':False,'blocked_reason':'known_conflict'}
        client=PlatformClient(PlatformTarget('https://platform.test','c'*43),transport=httpx.MockTransport(lambda _:reply(result)),clock=lambda:NOW)
        try:
            result=await PolicyEligibilityClient(client).eligibility('signed.fixture',payload,**values)
            assert result.allow is False and result.blocked_reason=='known_conflict'
        finally:await client.close()
    asyncio.run(run())
