"""FE-CHECK-02 current built cross-role knowledge/RAG journey; no paid calls."""
import argparse,asyncio,json,logging,secrets
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
from fastapi import Request
from starlette.responses import FileResponse,JSONResponse
from starlette.staticfiles import StaticFiles
from check_frontend_chat_turns import Harness as ChatHarness
from check_knowledge_workspace import verify
from frontend_browser_fixture import run_browser
from check_frontend_organization import ROOT,frontend_hashes
from enterprise_ai_platform.rag_check import Models
from enterprise_ai_platform.composition import ApplicationServices,build_rag_turn,database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.conversations import PostgresConversationRepository
from enterprise_ai_platform.infrastructure.database.identity import PostgresIdentityRepository
from enterprise_ai_platform.infrastructure.database.organization import PostgresOrganizationRepository
from enterprise_ai_platform.modules.conversations.service import ConversationService
from enterprise_ai_platform.modules.identity.service import IdentityService
from enterprise_ai_platform.modules.organization.service import OrganizationService
from enterprise_ai_platform.modules.files.service import FilesService
from enterprise_ai_platform.integrations.checkpoints.rag_graph import build_rag_graph
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.knowledge_check import write_json
from enterprise_ai_platform.main import create_app
OUT=ROOT/'local/fe-check-02'

class Harness(ChatHarness):
 async def rag_checkpoint(self):
  self.conversations=ConversationService(PostgresConversationRepository(self.database,live_sources=True),ORIGIN)
  control=secrets.token_urlsafe(32);calls=[]
  def app_for(origin):
   self.knowledge.origin=origin;self.conversations.origin=origin
   identity=IdentityService(SimpleNamespace(public_origin=origin),PostgresIdentityRepository(self.database),None,None)
   services=ApplicationServices(health=SimpleNamespace(),identity=identity,organization=OrganizationService(PostgresOrganizationRepository(self.database),origin),files=FilesService(self.file_repo,self.store,origin),conversations=self.conversations,knowledge=self.knowledge)
   app=create_app(Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True}),services=services)
   async def page():return FileResponse(ROOT/'frontend/dist/index.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
   for path in ('/','/chat','/chat/{id}','/knowledge','/knowledge/upload','/knowledge/documents/{id}','/search','/appearance','/access','/tenants','/sources/{id}'):app.add_api_route(path,page,methods=['GET'],include_in_schema=False)
   async def fixture(request:Request):
    if request.headers.get('x-fixture-control')!=control:return JSONResponse({},status_code=404)
    if request.headers.get('x-fixture-action')=='process':
     claim=await self.scheduler.claim();kind=claim.get('job_type')
     if kind=='file.inspect':await self.inspector.process(claim)
     elif kind=='knowledge.ingest':await self.process_knowledge(claim)
     elif kind=='agent.resume':
      @contextmanager
      def factory(observer):
       model=Models(observer,'answer');yield SimpleNamespace(chat=model,embedding=model);calls.extend(model.calls)
      worker=await build_rag_turn(self.database,self.jobs,claim,model_factory=factory,mode='vector');await worker.run(build_rag_graph)
     else:return JSONResponse({},status_code=409)
     return JSONResponse({'processed':kind})
    return JSONResponse({},status_code=400)
   app.add_api_route('/__fixture/control',fixture,methods=['POST'],include_in_schema=False);app.mount('/assets',StaticFiles(directory=str(ROOT/'frontend/dist/assets')));return app
  proof=await run_browser(OUT,'frontend-rag-checkpoint-browser.mjs',app_for,dict(manager=self.people[0]['token'],employee=self.people[4]['token'],foreign=self.people[2]['token'],control=control),timeout=300)
  counts={name:await self.scalar(query) for name,query in {'files':'SELECT count(*) FROM {s}.files','documents':'SELECT count(*) FROM {s}.documents','versions':'SELECT count(*) FROM {s}.document_versions','human':"SELECT count(*) FROM {s}.messages WHERE author_kind='human'",'model':"SELECT count(*) FROM {s}.messages WHERE author_kind='model'",'runs':'SELECT count(*) FROM {s}.runs','agent_jobs':"SELECT count(*) FROM {s}.outbox_jobs WHERE job_type='agent.resume'",'feedback':'SELECT count(*) FROM {s}.feedback'}.items()}
  require(counts==dict(files=1,documents=1,versions=1,human=1,model=1,runs=1,agent_jobs=1,feedback=1),'single-admission-result-no-extra-work')
  require(await self.scalar("SELECT rating='down' AND reason IS NULL FROM {s}.feedback") is True,'optional-reason-null')
  require(calls==['embedding','chat'],'feedback-and-reconciliation-no-inference')
  require(await self.scalar("SELECT audience_mode='specific_people' FROM {s}.documents") is True,'current-audience-revoked')
  require(await self.scalar('SELECT count(*) FROM {s}.document_acl WHERE principal_id=%s',(self.people[4]['person'],))==0,'employee-no-grant')
  write_json(OUT/'journey-invariants.json',dict(passed=True,counts=counts,deterministic_model_calls=calls,feedback_reason=None,paid_calls=0))
  self.passed('one_current_build_manager_to_employee_rag_'+str(len(proof['checks']))+'_checks');self.passed('actual_file_knowledge_and_agent_jobs_no_duplicates_or_feedback_inference')

def main():
 logging.disable(logging.CRITICAL);p=argparse.ArgumentParser(description=__doc__);p.add_argument('--live',action='store_true');args=p.parse_args()
 if not args.live:p.error('Explicit --live required; owned existing test DB only.')
 try:
  cfg=load_settings(Path('.env'),require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(cfg.database_url.get_secret_value());direct=parse_target(cfg.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=20,operation='rag_checkpoint'))
  result['frontend_source_sha256']=frontend_hashes();write_json(OUT/'database-proof.json',result);print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','frontend_source_sha256')}))
 except Exception as error:print('RAG checkpoint verification failed: '+(str(error) if isinstance(error,CheckFailure) else type(error).__name__));raise SystemExit(1) from None
if __name__=='__main__':main()
