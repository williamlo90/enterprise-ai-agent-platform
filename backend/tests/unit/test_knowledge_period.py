import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import httpx
import pytest
from enterprise_ai_platform.composition import ApplicationServices
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.knowledge.service import KnowledgeService
from enterprise_ai_platform.modules.knowledge.domain import KnowledgeFailure

ORIGIN='https://period.example'
def values():return dict(file_id=str(uuid4()),key=str(uuid4()),title='Fictional policy',readers=[])
def headers():return {'origin':ORIGIN,'content-type':'application/json','x-csrf-token':'c'*43}

@pytest.mark.parametrize('dates',[
 {'valid_from':'2026-02-29'},{'valid_from':'0000-01-01'}, {'valid_from':'2026-1-01'},
 {'valid_from':20261105},{'valid_until':True},{'valid_until':'infinity'},
 {'valid_from':'2026-11-05T00:00:00Z'},{'valid_from':'2026-11-05','valid_until':'2026-11-05'},
 {'valid_from':'2026-11-06','valid_until':'2026-11-05'},{'effective_from':'2026-11-05'},
])
def test_period_rejects_noncanonical_invalid_or_empty_ranges_before_database(dates):
 repo=SimpleNamespace(call=AsyncMock())
 with pytest.raises(KnowledgeFailure):asyncio.run(KnowledgeService(repo,ORIGIN).create('s'*43,headers(),{**values(),**dates}))
 repo.call.assert_not_called()

@pytest.mark.parametrize('dates',[{}, {'valid_from':None,'valid_until':None},{'valid_from':'2024-02-29'},{'valid_until':'2027-01-01'},{'valid_from':'0001-01-01','valid_until':'9999-12-31'}])
def test_legacy_and_partial_periods_preserve_exact_material(dates):
 repo=SimpleNamespace(call=AsyncMock(return_value={'authority_expires_at':20,'valid_from':dates.get('valid_from'),'valid_until':dates.get('valid_until')}))
 result=asyncio.run(KnowledgeService(repo,ORIGIN,clock=lambda:10).create('s'*43,headers(),{**values(),**dates}))
 assert result==dict(valid_from=dates.get('valid_from'),valid_until=dates.get('valid_until'))
 for name,value in dates.items():assert repo.call.call_args.args[2][name]==value

def test_http_dates_are_optional_strict_iso_strings_and_unknown_aliases_rejected():
 instance=SimpleNamespace(create=AsyncMock(return_value={}),version=AsyncMock(return_value={}))
 async def run():
  app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=SimpleNamespace(),knowledge=instance))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
   path='/api/v1/knowledge/documents';h={**headers(),'cookie':'__Host-eai-session='+'s'*43}
   assert (await client.post(path,json=values(),headers=h)).status_code==202
   assert (await client.post(path,json={**values(),'valid_from':'2026-11-05'},headers=h)).status_code==202
   for extra in ({'valid_from':True},{'valid_until':'20261105'},{'effective_from':'2026-11-05'}):
    assert (await client.post(path,json={**values(),**extra},headers=h)).status_code==422
   assert (await client.post(path+'/'+str(uuid4())+'/versions',json={'file_id':str(uuid4()),'key':str(uuid4()),'expected_version':1,'valid_until':'2027-01-01'},headers=h)).status_code==202
 asyncio.run(run())
