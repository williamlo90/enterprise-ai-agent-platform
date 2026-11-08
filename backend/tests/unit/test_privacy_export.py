import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.privacy.registry import PrivacyRegistryService
from enterprise_ai_platform.modules.authorization.ports import AccessFailure
from enterprise_ai_platform.api.browser_context import mutation_target
HEADERS={'origin':'https://app.example.test','content-type':'application/json','x-csrf-token':'c'*43}

@pytest.mark.parametrize('extra',[{'region':'ap-southeast-1'},{'service_proofs':[]},{'expires_at':9999999999},{'actor_id':'forged'}])
def test_export_rejects_client_location_permission_or_expiry_claim(extra):
 async def run():
  repo=SimpleNamespace(call=AsyncMock());body=dict(key=str(uuid4()),version=1,content_digest='a'*64,policy_hash='b'*64,**extra)
  with pytest.raises(AccessFailure):await PrivacyRegistryService(repo,HEADERS['origin']).fence_call('s'*43,str(uuid4()),'export',body,HEADERS)
  repo.call.assert_not_awaited()
 asyncio.run(run())

def test_response_only_export_has_original_context_and_finite_deadline():
 async def run():
  repo=SimpleNamespace(call=AsyncMock(return_value=dict(document={'storage':'response_only'},authority_expires_at=1001)))
  body=dict(key=str(uuid4()),version=1,content_digest='a'*64,policy_hash='b'*64);target=str(uuid4());service=PrivacyRegistryService(repo,HEADERS['origin'],clock=lambda:1000)
  result=await service.fence_call('s'*43,target,'export',body,HEADERS)
  assert result['_privacy_deadline']==1001 and repo.call.await_args.args[1]=='export'
  assert mutation_target('POST','/api/v1/privacy/conversations/'+target+'/export')==('privacy_conversations','export')
  repo.call.return_value=dict(document={},authority_expires_at=1000)
  with pytest.raises(AccessFailure):await service.fence_call('s'*43,target,'export',body,HEADERS)
 asyncio.run(run())


def test_expired_download_is_not_normalized_to_service_unavailable():
 from enterprise_ai_platform.infrastructure.database.privacy_registry import checked
 from enterprise_ai_platform.modules.privacy.ports import PrivacyDownloadExpired
 from enterprise_ai_platform.api.errors import ERRORS
 with pytest.raises(PrivacyDownloadExpired) as failure:checked({'status':'expired'})
 assert failure.value.status==410 and ERRORS[410][0]=='expired'
