import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.privacy.registry import PrivacyRegistryService
from enterprise_ai_platform.modules.authorization.ports import AccessFailure
from enterprise_ai_platform.api.browser_context import mutation_target
HEADERS={'origin':'https://app.example.test','content-type':'application/json','x-csrf-token':'c'*43}

@pytest.mark.parametrize('change',[
 lambda p:p.update(fence_key=None),lambda p:p.update(fence_key='00000000-0000-0000-0000-000000000000'),
 lambda p:p.update(actor_id=str(uuid4())),lambda p:p.update(effect_not_applied=True),lambda p:p.update(key='bad'),
])
def test_release_does_not_accept_forged_scope_effect_proof_or_invalid_id(change):
 async def run():
  data=dict(key=str(uuid4()),fence_key=str(uuid4()));change(data);repo=SimpleNamespace(call=AsyncMock())
  with pytest.raises(AccessFailure):await PrivacyRegistryService(repo,HEADERS['origin']).fence_call('s'*43,str(uuid4()),'release',data,HEADERS)
  repo.call.assert_not_awaited()
 asyncio.run(run())

def test_release_binds_exact_request_target_fence_and_current_CSRF():
 async def run():
  repo=SimpleNamespace(call=AsyncMock(return_value=dict(released=True,authority_expires_at=1100)))
  data=dict(key=str(uuid4()),fence_key=str(uuid4()));target=str(uuid4());service=PrivacyRegistryService(repo,HEADERS['origin'],clock=lambda:1000)
  result=await service.fence_call('s'*43,target,'release',data,HEADERS)
  sent=repo.call.await_args.args;assert sent[1]=='release' and sent[2]['id']==target and sent[2]['fence_key']==data['fence_key'] and len(sent[2]['csrf_hash'])==64
  assert result['released'] and mutation_target('POST','/api/v1/privacy/conversations/'+target+'/deletion/cancel')==('privacy_conversations','release')
  with pytest.raises(AccessFailure):await service.fence_call('s'*43,target,'release',data,dict(HEADERS,origin='https://other.test'))
 asyncio.run(run())
