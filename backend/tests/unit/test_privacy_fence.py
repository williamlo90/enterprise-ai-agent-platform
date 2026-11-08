import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.privacy.registry import PrivacyRegistryService
from enterprise_ai_platform.modules.authorization.ports import AccessFailure
from enterprise_ai_platform.api.browser_context import read_target,mutation_target

HEADERS={'origin':'https://app.example.test','content-type':'application/json','x-csrf-token':'c'*43}
def payload():return dict(key=str(uuid4()),version=1,content_digest='a'*64,policy_hash='b'*64)

@pytest.mark.parametrize('change',[
 lambda p:p.update(version=True),lambda p:p.update(version=0),lambda p:p.update(policy_hash='x'*64),
 lambda p:p.update(actor_id=str(uuid4())),lambda p:p.update(key='00000000-0000-0000-0000-000000000000'),
 lambda p:p.update(content_digest=None),lambda p:p.update(complete_stores=True),
])
def test_fence_never_trusts_user_authority_store_proofs_or_invalid_snapshot(change):
 async def run():
  data=payload();change(data);repo=SimpleNamespace(call=AsyncMock())
  with pytest.raises(AccessFailure):await PrivacyRegistryService(repo,HEADERS['origin']).fence_call('s'*43,str(uuid4()),'fence',data,HEADERS)
  repo.call.assert_not_awaited()
 asyncio.run(run())

def test_fence_uses_current_session_csrf_exact_target_and_deadline():
 async def run():
  repo=SimpleNamespace(call=AsyncMock(return_value=dict(fenced=True,erased=False,authority_expires_at=1100)))
  service=PrivacyRegistryService(repo,HEADERS['origin'],clock=lambda:1000);target=str(uuid4());data=payload()
  result=await service.fence_call('s'*43,target,'fence',data,HEADERS)
  sent=repo.call.await_args.args;assert sent[1]=='fence' and sent[2]['id']==target and len(sent[2]['csrf_hash'])==64
  assert result['erased'] is False and result['_privacy_deadline']==1100
  for h in [dict(HEADERS,origin='https://other.test'),dict(HEADERS,**{'x-csrf-token':''})]:
   with pytest.raises(AccessFailure):await service.fence_call('s'*43,target,'fence',data,h)
  repo.call.return_value=dict(authority_expires_at=999)
  with pytest.raises(AccessFailure):await service.fence_call('s'*43,target,'fence_status',{})
 asyncio.run(run())

def test_fence_and_status_have_original_request_G13_targets():
 path='/api/v1/privacy/conversations/'+str(uuid4())+'/deletion'
 assert mutation_target('POST',path)==('privacy_conversations','fence')
 assert read_target(path)==('privacy_conversations','fence_status')
