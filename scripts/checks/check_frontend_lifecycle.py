"""FE-KNOW-03 admission/lifecycle browser/API proof on owned restricted DB; no provider calls."""
import argparse
import asyncio
import json
import logging
import secrets
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID,uuid4
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

OUT=ROOT/'local/fe-know-03'

from enterprise_ai_platform.modules.knowledge.domain import KnowledgeFailure
class Harness(WorkspaceHarness):
 async def lifecycle_browser(self):
  tenant=self.people[0]['tenant'];scope=uuid4();person=uuid4();control=secrets.token_urlsafe(32)
  await self.execute("INSERT INTO {s}.authorization_scopes(tenant_id,scope_kind,scope_ref) VALUES(%s,'knowledge_collection',%s)",(tenant,scope))
  await self.member(tenant,person,grants=(RoleGrant('knowledge_manager','knowledge_collection',str(scope)),));scoped_token,_=await self.session(tenant,person)
  await self.execute("INSERT INTO {s}.organization_profiles(tenant_id,membership_id,display_name) VALUES(%s,%s,'Pengelola dokumen')",(tenant,self.people[0]['member']))
  def app_for(origin):
   identity=IdentityService(SimpleNamespace(public_origin=origin),PostgresIdentityRepository(self.database),None,None)
   organization=OrganizationService(PostgresOrganizationRepository(self.database),origin)
   files=FilesService(self.file_repo,self.store,origin);self.knowledge.origin=origin
   app=create_app(Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True}),services=ApplicationServices(health=SimpleNamespace(),identity=identity,organization=organization,knowledge=self.knowledge,files=files))
   async def page():return FileResponse(ROOT/'frontend/dist/index.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
   for route in ('/','/knowledge','/knowledge/upload','/knowledge/documents/{document_id}','/chat','/access','/appearance','/tenants'):app.add_api_route(route,page,methods=['GET'],include_in_schema=False)
   async def process(request:Request):
    if request.headers.get('x-fixture-control')!=control:return JSONResponse({},status_code=404)
    claim=await self.scheduler.claim()
    if claim.get('job_type')=='knowledge.ingest':
     if request.headers.get('x-fixture-fail')=='parse':
      class FailedExtractor:
       ref=self.extractor.ref
       called=False
       async def extract(self,data,mime):
        self.called=True
        raise KnowledgeFailure(422)
      extractor=FailedExtractor()
      try:await self.process_knowledge(claim,extractor=extractor)
      except KnowledgeFailure:pass
      require(extractor.called,'controlled-extractor-must-run-after-valid-begin')
      failed=await self.scalar("SELECT parse_state='failed' AND index_state='failed' FROM {s}.document_versions WHERE job_id=%s",(UUID(claim['id']),))
      require(failed is True,'controlled-extractor-failure-state-must-commit')
      return JSONResponse({'processed':True,'expected_failure':True})
     else:await self.process_knowledge(claim)
    elif claim.get('job_type')=='file.inspect':await self.inspector.process(claim)
    else:return JSONResponse({},status_code=409)
    return JSONResponse({'processed':True})
   app.add_api_route('/__fixture/process',process,methods=['POST'],include_in_schema=False)
   app.mount('/assets',StaticFiles(directory=str(ROOT/'frontend/dist/assets')));return app
  proof=await run_browser(OUT,'frontend-lifecycle-browser.mjs',app_for,dict(manager=self.people[0]['token'],scoped=scoped_token,employee=self.people[4]['token'],admin=self.people[3]['token'],foreign=self.people[2]['token'],scope=str(scope),control=control))
  require(await self.scalar('SELECT count(*) FROM {s}.documents')==3,'three-unique-document-admissions')
  require(await self.scalar('SELECT count(*) FROM {s}.document_versions')==4,'four-unique-version-admissions')
  require(await self.scalar('SELECT count(*) FROM {s}.documents WHERE withdrawn_at IS NOT NULL')==1,'one-terminal-withdrawal')
  require(await self.scalar("SELECT count(*) FROM {s}.document_versions WHERE parse_state='failed' OR index_state='failed'")==1,'one-controlled-ingestion-failure')
  self.passed('lifecycle_browser_actual_restricted_api_'+str(len(proof['checks']))+'_checks')
  self.passed('admission_and_lifecycle_unknown_replays_do_not_duplicate_documents_or_versions')

def main():
 logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
 if not args.live:parser.error('Explicit --live required; only owned fixtures are changed.')
 try:
  settings=load_settings(Path('.env'),require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=20,operation='lifecycle_browser'))
  result.update(document_count=3,version_count=4,withdrawn_document_count=1,failed_version_count=1)
  result['frontend_source_sha256']=frontend_hashes();write_json(OUT/'database-proof.json',result);print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','frontend_source_sha256')}))
 except Exception as error:print('Lifecycle browser verification failed: '+(str(error) if isinstance(error,CheckFailure) else type(error).__name__));raise SystemExit(1) from None

if __name__=='__main__':main()
