"""FE-KNOW-02 browser/API proof on owned restricted DB; no provider calls."""
import argparse
import asyncio
import json
import logging
import secrets
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
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
from enterprise_ai_platform.infrastructure.database.organization import PostgresOrganizationRepository
from enterprise_ai_platform.modules.identity.service import IdentityService
from enterprise_ai_platform.modules.organization.service import OrganizationService
from enterprise_ai_platform.modules.files.service import FilesService
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.knowledge_check import write_json

OUT=ROOT/'local/fe-know-02'

class Harness(WorkspaceHarness):
 async def upload_browser(self):
  pending=await self.document('Fictional existing draft. Local processing fixture.',ready=False)
  scope=uuid4();tenant=self.people[0]['tenant'];person=uuid4()
  await self.execute("INSERT INTO {s}.authorization_scopes(tenant_id,scope_kind,scope_ref) VALUES(%s,'knowledge_collection',%s)",(tenant,scope))
  await self.member(tenant,person,grants=(RoleGrant('knowledge_manager','knowledge_collection',str(scope)),))
  scoped_token,_=await self.session(tenant,person)
  control=secrets.token_urlsafe(32)
  before_docs=await self.scalar('SELECT count(*) FROM {s}.documents')
  def app_for(origin):
   identity=IdentityService(SimpleNamespace(public_origin=origin),PostgresIdentityRepository(self.database),None,None)
   organization=OrganizationService(PostgresOrganizationRepository(self.database),origin)
   files=FilesService(self.file_repo,self.store,origin)
   app=create_app(Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True}),services=ApplicationServices(health=SimpleNamespace(),identity=identity,organization=organization,knowledge=self.knowledge,files=files))
   async def page():return FileResponse(ROOT/'frontend/dist/index.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
   for route in ('/','/knowledge','/knowledge/upload','/knowledge/documents/{document_id}','/chat','/access','/appearance','/tenants'):app.add_api_route(route,page,methods=['GET'],include_in_schema=False)
   # Owned fixture control only; never installed in the application or artifact.
   # Each call runs one real queued job with controlled external adapters.
   async def process(request:Request):
    if request.headers.get('x-fixture-control')!=control:return JSONResponse({},status_code=404)
    claim=await self.scheduler.claim()
    if claim.get('job_type')=='knowledge.ingest':await self.process_knowledge(claim)
    elif claim.get('job_type')=='file.inspect':await self.inspector.process(claim)
    else:return JSONResponse({},status_code=409)
    return JSONResponse({'processed':True})
   app.add_api_route('/__fixture/process',process,methods=['POST'],include_in_schema=False)
   app.mount('/assets',StaticFiles(directory=str(ROOT/'frontend/dist/assets')));return app
  proof=await run_browser(OUT,'frontend-upload-browser.mjs',app_for,dict(manager=self.people[0]['token'],scoped=scoped_token,employee=self.people[4]['token'],admin=self.people[3]['token'],foreign=self.people[2]['token'],document=pending['document_id'],version=pending['version_id'],scope=str(scope),control=control))
  require(await self.scalar('SELECT count(*) FROM {s}.documents')==before_docs,'upload-does-not-admit-documents')
  require(await self.scalar("SELECT count(*) FROM {s}.files WHERE mime='text/markdown'")==5,'exactly-five-markdown-intents-including-lost-reply')
  self.passed('browser_upload_and_status_actual_restricted_api_'+str(len(proof['checks']))+'_checks')
  self.passed('upload_does_not_create_document_or_duplicate_lost_reply_intent')

def main():
 logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
 if not args.live:parser.error('Explicit --live required; only owned fixtures are changed.')
 try:
  settings=load_settings(Path('.env'),require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=20,operation='upload_browser'))
  result['frontend_source_sha256']=frontend_hashes();write_json(OUT/'database-proof.json',result);print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','frontend_source_sha256')}))
 except Exception as error:print('Upload browser verification failed: '+(str(error) if isinstance(error,CheckFailure) else type(error).__name__));raise SystemExit(1) from None

if __name__=='__main__':main()
