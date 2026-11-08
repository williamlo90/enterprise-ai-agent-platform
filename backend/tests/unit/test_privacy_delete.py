import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.privacy.registry import PrivacyRegistryService
from enterprise_ai_platform.modules.authorization.ports import AccessFailure
from enterprise_ai_platform.api.browser_context import mutation_target
H={'origin':'https://app.example.test','content-type':'application/json','x-csrf-token':'c'*43}
@pytest.mark.parametrize('extra',[{'complete_stores':True},{'backup':'clear'},{'original_session_id':str(uuid4())},{'region':'ap-southeast-1'}])
def test_browser_cannot_supply_completeness_location_or_original_authority(extra):
 async def run():
  repo=SimpleNamespace(call=AsyncMock())
  with pytest.raises(AccessFailure):await PrivacyRegistryService(repo,H['origin']).fence_call('s'*43,str(uuid4()),'delete',dict(key=str(uuid4()),fence_key=str(uuid4()),**extra),H)
  repo.call.assert_not_awaited()
 asyncio.run(run())
def test_execute_binds_current_context_and_exact_fence():
 async def run():
  repo=SimpleNamespace(call=AsyncMock(return_value=dict(content_deleted=True,erased=False,authority_expires_at=2000)));target=str(uuid4());key=str(uuid4());fence=str(uuid4())
  result=await PrivacyRegistryService(repo,H['origin'],clock=lambda:1000).fence_call('s'*43,target,'delete',dict(key=key,fence_key=fence),H)
  assert result['content_deleted'] and not result['erased'] and repo.call.await_args.args[2]['fence_key']==fence
  assert mutation_target('POST','/api/v1/privacy/conversations/'+target+'/deletion/execute')==('privacy_conversations','delete')
 asyncio.run(run())
