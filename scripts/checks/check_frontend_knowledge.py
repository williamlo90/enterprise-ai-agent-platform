"""FE-KNOW-01 scoped catalog/version browser proof; disposable fixtures only."""
import argparse
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID,uuid4
from starlette.responses import FileResponse
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
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.knowledge_check import write_json

OUT=ROOT/'local/fe-know-01'

class Harness(WorkspaceHarness):
 async def catalog_browser(self):
  row=await self.document('Fictional catalog policy; no real employee data.');require((await self.mutate(row,'publish')).status_code==200,'fixture-publish')
  replacement=await self.new_version(row,'Fictional draft replacement.');did=UUID(row['document_id']);tenant=self.people[0]['tenant']
  await self.execute("UPDATE {s}.documents SET title='Kebijakan perjalanan perusahaan' WHERE id=%s",(did,))
  # Pagination rows are labelled metadata fixtures, not simulated successful
  # ingestion of twenty files. The main document above used real controlled jobs.
  for number in range(3,23):
   await self.execute("INSERT INTO {s}.document_versions SELECT (jsonb_populate_record(NULL::{s}.document_versions,to_jsonb(v)||jsonb_build_object('id',%s::uuid,'version_no',%s::int,'parse_state','pending','index_state','pending','index_generation',0,'chunk_count',NULL,'parser_ref',NULL,'content_digest',NULL))).* FROM {s}.document_versions v WHERE id=%s",(uuid4(),number,UUID(replacement['version_id'])))
  for number in range(21):
   clone=uuid4();clone_version=uuid4()
   await self.execute("INSERT INTO {s}.documents SELECT (jsonb_populate_record(NULL::{s}.documents,to_jsonb(d)||jsonb_build_object('id',%s::uuid,'title',%s::text,'active_version_id',NULL,'version',1,'created_at',clock_timestamp()-interval '1 day'))).* FROM {s}.documents d WHERE id=%s",(clone,'Pagination draft '+str(number+1),did))
   await self.execute("INSERT INTO {s}.document_versions SELECT (jsonb_populate_record(NULL::{s}.document_versions,to_jsonb(v)||jsonb_build_object('id',%s::uuid,'document_id',%s::uuid,'version_no',1,'publication_state','draft','parse_state','pending','index_state','pending','index_generation',0,'chunk_count',NULL,'parser_ref',NULL,'content_digest',NULL))).* FROM {s}.document_versions v WHERE id=%s",(clone_version,clone,UUID(replacement['version_id'])))
  scopes=[uuid4() for _ in range(22)]
  for scope in scopes:await self.execute("INSERT INTO {s}.authorization_scopes(tenant_id,scope_kind,scope_ref) VALUES(%s,'knowledge_collection',%s)",(tenant,scope))
  scope=scopes[0]
  async with await self.client() as client:
   upload=await client.post('/api/v1/files?purpose=knowledge&collection_id='+str(scope),content=b'Fictional scoped draft.',headers=self.headers())
  require(upload.status_code==202,'scope-upload');await super().process()
  scoped=await self.create_document(upload.json()['id'],title='Dokumen koleksi terbatas');require(scoped.status_code==202,'scoped-admission');await self.process_knowledge()
  person=uuid4();await self.member(tenant,person,grants=(RoleGrant('knowledge_manager','knowledge_collection',str(scope)),));scoped_token,_=await self.session(tenant,person)
  same_owner,_=await self.session(tenant,self.people[0]['person'])
  def app_for(origin):
   identity=IdentityService(SimpleNamespace(public_origin=origin),PostgresIdentityRepository(self.database),None,None)
   organization=OrganizationService(PostgresOrganizationRepository(self.database),origin)
   app=create_app(Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True}),services=ApplicationServices(health=SimpleNamespace(),identity=identity,organization=organization,knowledge=self.knowledge))
   async def page():return FileResponse(ROOT/'frontend/dist/index.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
   for route in ('/','/knowledge','/knowledge/documents/{document_id}','/organization','/chat','/access','/appearance','/tenants'):app.add_api_route(route,page,methods=['GET'],include_in_schema=False)
   app.mount('/assets',StaticFiles(directory=str(ROOT/'frontend/dist/assets')));return app
  proof=await run_browser(OUT,'frontend-knowledge-browser.mjs',app_for,dict(manager=self.people[0]['token'],newSession=same_owner,scoped=scoped_token,foreign=self.people[2]['token'],employee=self.people[4]['token'],document=str(did),scope=str(scope),scopedDocument=scoped.json()['document_id']))
  self.passed('built_knowledge_catalog_browser_passes_'+str(len(proof['checks']))+'_checks_actual_restricted_api')

def main():
 logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
 if not args.live:parser.error('Explicit --live required; only owned fixtures are changed.')
 try:
  settings=load_settings(Path('.env'),require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=20,operation='catalog_browser'))
  result['frontend_source_sha256']=frontend_hashes();write_json(OUT/'database-proof.json',result);print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','frontend_source_sha256')}))
 except Exception as error:print('Knowledge browser verification failed: '+(str(error) if isinstance(error,CheckFailure) else type(error).__name__));raise SystemExit(1) from None

if __name__=='__main__':main()
