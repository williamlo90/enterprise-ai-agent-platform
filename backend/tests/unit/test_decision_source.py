import asyncio
import hashlib
import hmac
import json
from uuid import uuid4
import httpx
import pytest
from enterprise_ai_platform.integrations.reimbursement.decision_source import DecisionSourceReader
from enterprise_ai_platform.integrations.reimbursement.policies import PolicyTarget
from enterprise_ai_platform.integrations.security.receipt_settlement import SettlementKey
from enterprise_ai_platform.modules.actions.domain import ActionFailure,encoded
from enterprise_ai_platform.modules.delegation.domain import request_hash


def uid():return str(uuid4())


async def run(change=lambda value:None):
    context=dict(tenant_id=uid(),actor_id=uid(),membership_id=uid(),session_id=uid(),access_revision=1,authority_expires_at=160);cid=uid();signed=[]
    class Signer:
        issuer='https://issuer.test';audience='reference-reimbursement';caller='platform-dispatcher'
        def sign(self,claims,now):signed.append(claims);return 'test-token'
    def respond(request):
        p=json.loads(request.content)['payload']
        value=dict(tenant_id=context['tenant_id'],actor_id=context['actor_id'],operation_id=p['operation_id'],request_hash=request_hash(p),checked_at=100,expires_at=150,
            claim=dict(id=cid,owner_id=uid(),trip_id=uid(),trip_date='2026-11-06',state='pending_approval',version=1,revision=1,active_stage=1,reviewer_id=context['actor_id'],policy=dict(id=uid(),version=1,digest='a'*64)))
        change(value)
        return httpx.Response(200,json=value)
    reader=DecisionSourceReader(PolicyTarget('https://source.test','d'*43),Signer(),SettlementKey('test',b'x'*32,90,200),'c'*64,transport=httpx.MockTransport(respond),clock=lambda:100)
    try:return (await reader.read(context,cid)).wire(),signed,context
    finally:await reader.close()


def test_current_review_metadata_is_bound_to_original_context_with_independent_MAC():
    wire,signed,context=asyncio.run(run())
    assert wire['material']['context']=={k:v for k,v in context.items() if k!='authority_expires_at'}
    assert wire['proof']==hmac.new(b'x'*32,b'eai:decision-source:v1:'+encoded(wire['material']),hashlib.sha256).hexdigest()
    assert signed[0]['action']=='claim.read' and signed[0]['human_receipt_id'] is None


@pytest.mark.parametrize('change',[
    lambda v:v.update(actor_id=uid()),lambda v:v.update(operation_id=uid()),lambda v:v.update(request_hash='f'*64),
    lambda v:v.update(expires_at=100),lambda v:v.update(expires_at=161),lambda v:v.update(checked_at=True),
    lambda v:v['claim'].update(active_stage=True),lambda v:v['claim'].update(reviewer_id=uid()),
    lambda v:v['claim'].update(owner_id=v['actor_id']),lambda v:v['claim'].update(state='approved'),
    lambda v:v['claim']['policy'].update(digest='A'*64),
])
def test_invalid_current_source_never_becomes_private_decision_authority(change):
    with pytest.raises(ActionFailure):asyncio.run(run(change))
