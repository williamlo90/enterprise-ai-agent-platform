"""FE-KNOW-04 audience browser/API proof on owned restricted DB; no provider calls."""
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

OUT=ROOT/'local/fe-know-04'

class Harness(WorkspaceHarness):
 async def visual_browser(self):
  self.visual_only=True
  await self.audience_browser()
 async def audience_browser(self):
  document=await self.document('Fictional audience policy. Controlled ports only.')
  require((await self.mutate(document,'publish')).status_code==200,'fixture-publish')
  tenant=self.people[0]['tenant'];control=secrets.token_urlsafe(32);members=[]
  for n in range(4 if getattr(self,'visual_only',False) else 52):
   person=uuid4();member=await self.member(tenant,person,grants=(RoleGrant('employee','tenant',str(tenant)),))
   await self.execute("INSERT INTO {s}.organization_profiles(tenant_id,membership_id,display_name,department,job_title) VALUES(%s,%s,%s,%s,'Manager')",(tenant,member,'Finance reader '+str(n+1).zfill(2),'Finance' if n<51 else 'HR'))
   members.append((person,member))
  for index,name in ((0,'Pengelola dokumen'),(4,'Pembaca nonaktif')):
   await self.execute("INSERT INTO {s}.organization_profiles(tenant_id,membership_id,display_name) VALUES(%s,%s,%s)",(tenant,self.people[index]['member'],name))
  await self.execute('UPDATE {s}.memberships SET active=false WHERE tenant_id=%s AND id=%s',(tenant,self.people[4]['member']))
  reader_token,_=await self.session(tenant,members[0][0])
  scope=uuid4();scoped=uuid4()
  await self.execute("INSERT INTO {s}.authorization_scopes(tenant_id,scope_kind,scope_ref) VALUES(%s,'knowledge_collection',%s)",(tenant,scope))
  await self.member(tenant,scoped,grants=(RoleGrant('knowledge_manager','knowledge_collection',str(scope)),));scoped_token,_=await self.session(tenant,scoped)
  def app_for(origin):
   identity=IdentityService(SimpleNamespace(public_origin=origin),PostgresIdentityRepository(self.database),None,None)
   organization=OrganizationService(PostgresOrganizationRepository(self.database),origin)
   self.knowledge.origin=origin
   app=create_app(Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True}),services=ApplicationServices(health=SimpleNamespace(),identity=identity,organization=organization,knowledge=self.knowledge))
   async def page():return FileResponse(ROOT/'frontend/dist/index.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
   for route in ('/','/knowledge','/knowledge/documents/{document_id}','/chat','/access','/appearance','/tenants'):app.add_api_route(route,page,methods=['GET'],include_in_schema=False)
   async def drift(request:Request):
    if request.headers.get('x-fixture-control')!=control:return JSONResponse({},status_code=404)
    await self.execute("UPDATE {s}.organization_profiles SET job_title='Director',version=version+1 WHERE tenant_id=%s AND membership_id=%s",(tenant,members[0][1]))
    return JSONResponse({'changed':True})
   app.add_api_route('/__fixture/drift',drift,methods=['POST'],include_in_schema=False)
   app.mount('/assets',StaticFiles(directory=str(ROOT/'frontend/dist/assets')));return app
  proof=await run_browser(OUT/'visual' if getattr(self,'visual_only',False) else OUT,'frontend-audience-browser.mjs',app_for,dict(visualOnly=getattr(self,'visual_only',False),manager=self.people[0]['token'],reader=reader_token,scoped=scoped_token,admin=self.people[3]['token'],foreign=self.people[2]['token'],document=document['document_id'],version=document['version_id'],control=control))
  if getattr(self,'visual_only',False):
   self.passed('visual_only_audience_layout_'+str(len(proof['checks']))+'_checks_not_business_regression')
   return
  current=await self.knowledge.audience(self.people[0]['token'],document['document_id'])
  require(current['audience_mode']=='all_current_employees' and current['explicit_readers_count']==0,'final-all-employee-state')
  self.passed('audience_browser_actual_restricted_api_'+str(len(proof['checks']))+'_checks')
  self.passed('final_audience_transaction_is_all_employees_with_zero_explicit_grants')

def main():
 logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--visual-only',action='store_true');args=parser.parse_args()
 if not args.live:parser.error('Explicit --live required; only owned fixtures are changed.')
 try:
  settings=load_settings(Path('.env'),require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=20,operation='visual_browser' if args.visual_only else 'audience_browser'))
  result['frontend_source_sha256']=frontend_hashes();write_json((OUT/'visual' if args.visual_only else OUT)/'database-proof.json',result);print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','frontend_source_sha256')}))
 except Exception as error:print('Audience browser verification failed: '+(str(error) if isinstance(error,CheckFailure) else type(error).__name__));raise SystemExit(1) from None

if __name__=='__main__':main()
