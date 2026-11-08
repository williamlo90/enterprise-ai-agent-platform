import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import httpx
import pytest
from enterprise_ai_platform.api.browser_context import read_target
from enterprise_ai_platform.composition import ApplicationServices
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.knowledge.service import KnowledgeService
from enterprise_ai_platform.modules.knowledge.domain import KnowledgeFailure

ORIGIN='https://workspace.example'
@pytest.mark.parametrize('values',[{'limit':0},{'limit':True},{'limit':51},{'after':'bad'},{'query':''},{'query':'漢'*267},{'query':'bad\x00'},{'query':'\ud800'},{'collection':'bad'}])
def test_catalog_input_bounded_and_no_repository_call(values):
 repo=SimpleNamespace(call=AsyncMock())
 with pytest.raises(KnowledgeFailure):asyncio.run(KnowledgeService(repo,ORIGIN).catalog('s'*43,**values))
 repo.call.assert_not_called()

def test_preview_discards_first_buffer_and_binds_exact_generation():
 did,vid=str(uuid4()),str(uuid4());repo=SimpleNamespace(call=AsyncMock(side_effect=[
  dict(authority_expires_at=20,version_id=vid,access_revision=2,index_generation=3,content_digest='a'*64,chunks=['stale']),
  dict(authority_expires_at=20,chunks=['fresh'])]))
 result=asyncio.run(KnowledgeService(repo,ORIGIN,clock=lambda:10).preview('s'*43,did,vid))
 assert result==dict(chunks=['fresh'])
 assert repo.call.call_args.args[1:] == ('preview_recheck',dict(id=did,version_id=vid,access_revision=2,index_generation=3,content_digest='a'*64))

@pytest.mark.parametrize('suffix,op',[('', 'catalog'),('/a','detail'),('/a/versions','versions'),('/a/versions/b/preview','preview_recheck')])
def test_private_workspace_gets_map_to_exact_final_attestation(suffix,op):
 assert read_target('/api/v1/knowledge/documents'+suffix)==('knowledge',op)

def test_http_workspace_rejects_unknown_duplicate_and_conditional_queries():
 instance=SimpleNamespace(catalog=AsyncMock(return_value=dict(items=[],next_after=None)),detail=AsyncMock(return_value={}),versions=AsyncMock(return_value={}),preview=AsyncMock(return_value={}))
 async def run():
  app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=SimpleNamespace(),knowledge=instance))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
   base='/api/v1/knowledge/documents';h={'cookie':'__Host-eai-session='+'s'*43}
   for query in ('?tenant=x','?limit=01','?limit=2&limit=3','?q=x&owner=x'):
    assert (await client.get(base+query,headers=h)).status_code==400
   assert (await client.get(base+'?q=%25&collection=tenant',headers=h)).status_code==200
   assert instance.catalog.call_args.kwargs==dict(after=None,limit=20,query='%',collection='tenant')
   assert (await client.get(base+'/a/versions/b/preview',headers={**h,'range':'bytes=0-10'})).status_code==400
   instance.preview.assert_not_called()
 asyncio.run(run())
