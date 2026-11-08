import asyncio
import base64
import hashlib
import json
from uuid import uuid4
import httpx
import pytest
from enterprise_ai_platform.integrations.reimbursement.workflow import WorkflowTransport
from enterprise_ai_platform.integrations.reimbursement.policies import PolicyTarget
from enterprise_ai_platform.modules.delegation.domain import request_hash
from enterprise_ai_platform.modules.flows.domain import FlowFailure


def uid():return str(uuid4())


async def run(method,status=200,expired=False):
    ctx=dict(tenant_id=uid(),actor_id=uid(),membership_id=uid(),session_id=uid(),access_revision=1)
    op=uid();payload=dict(schema_version=1,action='claim.decide',tenant_id=ctx['tenant_id'],actor_id=ctx['actor_id'],operation_id=op,object=dict(kind='claim',id=uid(),version=2),data=dict(revision=1,stage=1,decision='approve',reason=''))
    identity=dict(tenant_id=ctx['tenant_id'],actor_id=ctx['actor_id'],operation_id=op,kind='claim.decide',key=uid(),request_hash=request_hash(payload),object_kind='claim',object_id=payload['object']['id'],object_version=2,caller='platform-dispatcher')
    packet=dict(context=ctx,identity=identity,payload=payload,receipt=dict(id=uid(),operation_id=op,session_id=ctx['session_id'],human_action_time=100,request_hash=identity['request_hash'],revoked=False),expires_at=120)
    captured=[];signed=[];now=[100]
    class Signer:
        issuer='enterprise-ai-platform';audience='reference-reimbursement';caller='platform-dispatcher'
        def sign(self,claims,now):signed.append(claims);return 'signed-test-token'
    def respond(request):
        captured.append(request)
        if expired:now[0]=121
        return httpx.Response(status,json={'operation':{},'proof':{}},headers={'location':'https://untrusted.test'})
    channel=WorkflowTransport(PolicyTarget('https://source.test','d'*43),Signer(),'r'*43,transport=httpx.MockTransport(respond),clock=lambda:now[0])
    try:return await getattr(channel,method)(packet),captured,signed,packet
    finally:await channel.close()


def test_execute_uses_original_canonical_action_key_receipt_and_no_browser_credentials():
    _,requests,signed,packet=asyncio.run(run('execute'))
    body=json.loads(requests[0].content)
    assert body==dict(payload=packet['payload'],key=packet['identity']['key'])
    assert signed[0]['human_receipt_id']==packet['receipt']['id'] and signed[0]['session_id']==packet['context']['session_id']
    assert requests[0].headers['authorization']=='Bearer '+'d'*43 and 'cookie' not in requests[0].headers


@pytest.mark.parametrize('method',['lookup','seal'])
def test_read_and_recovery_grants_are_narrow_and_do_not_renew_mutation_consent(method):
    _,requests,signed,packet=asyncio.run(run(method));claims=signed[0]
    assert claims['action']=='operation.read' and claims['human_receipt_id'] is None and claims['object_id']==packet['identity']['operation_id']
    assert claims['sub']==packet['context']['actor_id'] and claims['session_id']==packet['context']['session_id']
    assert requests[0].headers['authorization']=='Bearer '+('r' if method=='seal' else 'd')*43
    if method=='seal':assert json.loads(requests[0].content)['identity']==packet['identity']


def test_lookup404_is_only_absence_and_does_not_manufacture_seal_proof():
    result,requests,_,_=asyncio.run(run('lookup',404));assert result is None and len(requests)==1


@pytest.mark.parametrize('status',[307,403,503])
def test_errors_and_redirects_do_not_retry_or_forward_authority(status):
    with pytest.raises(FlowFailure):asyncio.run(run('execute',status))


def test_response_after_original_grant_deadline_is_discarded():
    with pytest.raises(FlowFailure):asyncio.run(run('execute',expired=True))
