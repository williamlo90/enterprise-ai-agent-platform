"""Source protocol rejects mismatched facts before producing private SQL MAC."""
import asyncio
import copy
import hashlib
import hmac
import json
from types import SimpleNamespace
from uuid import uuid4
import httpx
import pytest
from enterprise_ai_platform.integrations.reimbursement.draft_sources import DraftSourceReader
from enterprise_ai_platform.integrations.reimbursement.policies import PolicyTarget
from enterprise_ai_platform.integrations.security.receipt_settlement import SettlementKey
from enterprise_ai_platform.modules.drafts.domain import DraftFailure,encoded
from enterprise_ai_platform.modules.delegation.domain import request_hash


def uid():return str(uuid4())


def context():return dict(tenant_id=uid(),actor_id=uid(),membership_id=uid(),session_id=uid(),access_revision=1,authority_expires_at=160)


async def run(change=lambda v:None,*,claim=False):
    ctx=context();trip=uid();claim_id=uid() if claim else None;captured=[]
    class Signer:
        issuer='https://issuer.test';audience='reference-reimbursement';caller='platform-dispatcher'
        def sign(self,claims,now):captured.append(copy.deepcopy(claims));return 'fixture-signed-token'
    def respond(request):
        body=json.loads(request.content);payload=body['payload']
        value=dict(tenant_id=ctx['tenant_id'],actor_id=ctx['actor_id'],operation_id=payload['operation_id'],request_hash=request_hash(payload),
            trip=dict(id=trip,start_date='2026-11-05',end_date='2026-11-06',state='completed',employee_ref='Fictional',items=[dict(id=uid(),expense_date='2026-10-01',category='travel',declared_amount_minor=500,currency='SGD')]),
            claim=None if not claim else dict(id=claim_id,owner_id=ctx['actor_id'],trip_id=trip,state='needs_information',version=2,revision=1,policy=dict(id=uid(),version=1,digest='a'*64)),checked_at=100,expires_at=150)
        change(value)
        return httpx.Response(200,json=value)
    key=SettlementKey('draft',b'x'*32,90,200)
    reader=DraftSourceReader(PolicyTarget('https://source.test','f'*43),Signer(),key,'c'*64,transport=httpx.MockTransport(respond),clock=lambda:100)
    try:return (await reader.read(ctx,trip,claim_id)).wire(),captured,ctx
    finally:await reader.close()


@pytest.mark.parametrize('claim',[False,True])
def test_read_produces_exact_context_and_independent_domain_separated_mac(claim):
    wire,claims,ctx=asyncio.run(run(claim=claim))
    assert wire['material']['context']=={k:v for k,v in ctx.items() if k!='authority_expires_at'}
    assert wire['proof']==hmac.new(b'x'*32,b'eai:draft-source:v1:'+encoded(wire['material']),hashlib.sha256).hexdigest()
    assert claims[0]['action']==('claim.read' if claim else 'trip.read') and claims[0]['human_receipt_id'] is None


@pytest.mark.parametrize('change',[
    lambda v:v.update(tenant_id=uid()),lambda v:v.update(actor_id=uid()),
    lambda v:v.update(operation_id=uid()),lambda v:v.update(request_hash='f'*64),
    lambda v:v.update(checked_at=True),lambda v:v.update(checked_at=106),
    lambda v:v.update(expires_at=100),lambda v:v.update(expires_at=161),
    lambda v:v['trip'].update(id=uid()),lambda v:v['trip'].update(state='open'),
    lambda v:v['trip']['items'][0].update(declared_amount_minor=True),
    lambda v:v.update(claim={}),lambda v:v.update(extra='unexpected'),
])
def test_mismatched_or_expired_source_never_becomes_SQL_authority(change):
    with pytest.raises(DraftFailure) as error:asyncio.run(run(change))
    assert error.value.status==503


@pytest.mark.parametrize('change',[
    lambda v:v['claim'].update(owner_id=uid()),lambda v:v['claim'].update(state='pending_approval'),
    lambda v:v['claim'].update(version=True),lambda v:v['claim']['policy'].update(digest='A'*64),
])
def test_information_source_must_be_current_owner_needs_information_with_strict_policy(change):
    with pytest.raises(DraftFailure):asyncio.run(run(change,claim=True))
