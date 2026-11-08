import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from enterprise_ai_platform.composition import ApplicationServices
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.conversations.domain import ConversationFailure
from enterprise_ai_platform.modules.conversations.service import ConversationService
from enterprise_ai_platform.modules.identity.service import digest

ORIGIN='https://labels.example'
def headers():return {'origin':ORIGIN,'content-type':'application/json','x-csrf-token':'c'*43}
def values(**changes):return dict(name='Nama lengkap',expected_version=1,key=str(uuid4()),**changes)

@pytest.mark.parametrize('change',[
 {'name':''},{'name':' \r\n\t'},{'name':'x'*4097},{'name':'漢'*2731},{'name':'\ud800'},
 {'name':'hello\x7f'},{'name':'bad\x00'}, {'expected_version':True},{'expected_version':0},
 {'expected_version':10**18},{'key':'bad'},{'tenant_id':str(uuid4())},
])
def test_rename_rejects_invalid_material_before_repository(change):
 repo=SimpleNamespace(call=AsyncMock());service=ConversationService(repo,ORIGIN)
 payload=values();payload.update(change)
 with pytest.raises(ConversationFailure):asyncio.run(service.rename('s'*43,headers(),str(uuid4()),payload))
 repo.call.assert_not_called()

def test_rename_preserves_full_material_and_uses_only_session_authority():
 cid=str(uuid4());payload=values();payload['name']='完整'+('x'*4092)+'\n'
 repo=SimpleNamespace(call=AsyncMock(return_value=dict(id=cid,version=2,authority_expires_at=20)))
 result=asyncio.run(ConversationService(repo,ORIGIN,clock=lambda:10).rename('s'*43,headers(),cid,payload))
 assert result==dict(id=cid,version=2)
 assert repo.call.call_args.args==(digest('session','s'*43),'rename',{**payload,'id':cid,'csrf_hash':digest('csrf','c'*43)})

@pytest.mark.parametrize('changed', [{'origin':'https://foreign.example'},{'x-csrf-token':'bad'},{'content-type':'text/plain'}])
def test_rename_requires_browser_csrf(changed):
 repo=SimpleNamespace(call=AsyncMock());h=headers();h.update(changed)
 with pytest.raises(ConversationFailure):asyncio.run(ConversationService(repo,ORIGIN).rename('s'*43,h,str(uuid4()),values()))
 repo.call.assert_not_called()

def test_http_rename_is_strict_and_does_not_accept_query_or_owner():
 instance=SimpleNamespace(rename=AsyncMock(return_value=dict(id=str(uuid4()),version=2)))
 async def run():
  app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=SimpleNamespace(),conversations=instance))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
   path='/api/v1/conversations/'+str(uuid4())+'/name';h={**headers(),'cookie':'__Host-eai-session='+'s'*43}
   assert (await client.patch(path,json=values(),headers=h)).status_code==200
   assert (await client.patch(path+'?tenant=x',json=values(),headers=h)).status_code==400
   assert (await client.patch(path,json={**values(),'owner_id':str(uuid4())},headers=h)).status_code==422
   assert instance.rename.await_count==1
 asyncio.run(run())
