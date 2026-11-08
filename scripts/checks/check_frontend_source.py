"""FE-SOURCE-01 staged browser/API proof; owned DB, controlled ingestion, no cloud."""
import argparse
import asyncio
import json
import logging
import secrets
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
from fastapi import Request
from starlette.responses import FileResponse,JSONResponse
from starlette.staticfiles import StaticFiles
from check_knowledge_workspace import Harness as WorkspaceHarness,verify
from frontend_browser_fixture import run_browser
from check_frontend_organization import ROOT,frontend_hashes
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import ApplicationServices,database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.identity import PostgresIdentityRepository
from enterprise_ai_platform.modules.identity.service import IdentityService
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.knowledge_check import source_hashes,write_json

OUT=ROOT/'local/fe-source-01'

class Harness(WorkspaceHarness):
 async def source_browser(self):
  control=secrets.token_urlsafe(32)
  old=await self.document('😀 Biaya perjalanan lama. Fictional travel oldword policy.');require((await self.mutate(old,'publish')).status_code==200,'publish-old')
  current=await self.new_version(old,'😀 Biaya perjalanan terbaru. Fictional travel currentword policy.');require((await self.mutate(current,'publish')).status_code==200,'publish-current')
  draft=await self.document('Fictional unpublished hiddenword source.')
  await self.execute("UPDATE {s}.documents SET title='Kebijakan perjalanan — teks sumber lengkap' WHERE id=%s",(UUID(old['document_id']),))
  require((await self.source_http(old)).json()['publication_state']=='superseded','old-source-readable')
  require((await self.source_http(draft)).status_code==404,'draft-source-hidden')
  hits=await self.search_http('perjalanan',language='id');require(hits.status_code==200 and len(hits.json()['items'])==1 and hits.json()['items'][0]['version_id']==current['version_id'],'active-only-id-search')
  require(len((await self.search_http('currentword')).json()['items'])==1,'en-search')
  require((await self.search_http('oldword')).json()['items']==[],'superseded-not-searchable')
  for index in (2,3):require((await self.source_http(current,index)).status_code==404 and (await self.search_http('currentword',index=index)).json()['items']==[],'foreign-admin-no-default-source-access')
  async def invariant(stage):
   values=dict(documents=await self.scalar('SELECT count(*) FROM {s}.documents'),versions=await self.scalar('SELECT count(*) FROM {s}.document_versions'),superseded=await self.scalar("SELECT count(*) FROM {s}.document_versions WHERE publication_state='superseded'"))
   require(values==dict(documents=2,versions=3,superseded=1),'stable-source-fixture-invariants')
   write_json(OUT/(stage+'-database-proof.json'),dict(passed=True,stage=stage,invariants=values,source_sha256=source_hashes(),frontend_source_sha256=frontend_hashes(),paid_calls=0))
  await invariant('stage-a-api-positive')
  def app_for(origin):
   self.knowledge.origin=origin
   identity=IdentityService(SimpleNamespace(public_origin=origin),PostgresIdentityRepository(self.database),None,None)
   app=create_app(Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True}),services=ApplicationServices(health=SimpleNamespace(),identity=identity,knowledge=self.knowledge))
   async def page():return FileResponse(ROOT/'frontend/dist/index.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
   for route in ('/','/search','/sources/{version_id}','/chat','/access','/appearance','/tenants'):app.add_api_route(route,page,methods=['GET'],include_in_schema=False)
   async def stage(request:Request):
    if request.headers.get('x-fixture-control')!=control:return JSONResponse({},status_code=404)
    action=request.headers.get('x-fixture-action')
    if action=='checkpoint':await invariant('stage-b-browser-positive');return JSONResponse({'saved':True})
    headers={**self.headers(0,'application/json'),'origin':origin}
    if action in ('revoke','grant'):
     readers=[str(self.people[4]['person'])] if action=='grant' else []
     response=await self.mutate(current,'acl',readers=readers,extra_headers=headers)
    elif action=='withdraw':response=await self.mutate(current,'withdraw',extra_headers=headers)
    else:return JSONResponse({},status_code=400)
    require(response.status_code==200,'fixture-authorized-'+action)
    if action=='withdraw':require(await self.scalar('SELECT active_version_id IS NULL AND withdrawn_at IS NOT NULL FROM {s}.documents WHERE id=%s',(UUID(old['document_id']),)) is True,'withdraw-db-pointer')
    write_json(OUT/('stage-c-'+action+'-database-proof.json'),dict(passed=True,operation=action,paid_calls=0,source_sha256=source_hashes(),frontend_source_sha256=frontend_hashes()))
    return JSONResponse({'applied':True})
   app.add_api_route('/__fixture/stage',stage,methods=['POST'],include_in_schema=False)
   app.mount('/assets',StaticFiles(directory=str(ROOT/'frontend/dist/assets')));return app
  proof=await run_browser(OUT,'frontend-source-browser.mjs',app_for,dict(employee=self.people[4]['token'],admin=self.people[3]['token'],foreign=self.people[2]['token'],old=old['version_id'],current=current['version_id'],draft=draft['version_id'],control=control))
  require(await self.scalar('SELECT count(*) FROM {s}.documents WHERE withdrawn_at IS NOT NULL')==1,'one-withdrawal')
  self.passed('source_browser_actual_restricted_api_'+str(len(proof['checks']))+'_checks')

def main():
 logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
 if not args.live:parser.error('Explicit --live required; only owned fixtures are changed.')
 try:
  settings=load_settings(Path('.env'),require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=20,operation='source_browser'))
  result['frontend_source_sha256']=frontend_hashes();write_json(OUT/'database-proof.json',result);print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','frontend_source_sha256')}))
 except Exception as error:print('Source browser verification failed: '+(str(error) if isinstance(error,CheckFailure) else type(error).__name__));raise SystemExit(1) from None

if __name__=='__main__':main()
