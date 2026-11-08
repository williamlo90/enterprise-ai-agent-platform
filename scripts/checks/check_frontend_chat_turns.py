"""FE-CHAT-02 actual graph/DB/API/browser; deterministic models, no AWS calls."""
import argparse,asyncio,json,logging,secrets
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4,UUID
from fastapi import Request
from starlette.responses import FileResponse,JSONResponse
from starlette.staticfiles import StaticFiles
from check_knowledge_workspace import Harness as WorkspaceHarness,verify
from frontend_browser_fixture import run_browser
from check_frontend_organization import ROOT,frontend_hashes
from enterprise_ai_platform.rag_check import Harness as RagHarness,Models
from enterprise_ai_platform.composition import ApplicationServices,build_rag_turn,database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.conversations import PostgresConversationRepository
from enterprise_ai_platform.infrastructure.database.identity import PostgresIdentityRepository
from enterprise_ai_platform.modules.conversations.service import ConversationService
from enterprise_ai_platform.modules.identity.service import IdentityService
from enterprise_ai_platform.integrations.checkpoints.rag_graph import build_rag_graph
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.knowledge_check import write_json
from enterprise_ai_platform.main import create_app

OUT=ROOT/'local/fe-chat-02'
class Harness(WorkspaceHarness,RagHarness):
 async def chat_turns(self):
  self.conversations=ConversationService(PostgresConversationRepository(self.database,live_sources=True),ORIGIN)
  rows=[]
  for text in ['😀 Travel claims must be submitted within seven days under <b>fictional policy</b>.','Travel claims must be submitted within fourteen days under fictional alternative policy.']:
   row=await self.document(text);require((await self.mutate(row,'publish')).status_code==200,'publish');rows.append(row)
  created=await self.create_conversation(index=4,language='en');require(created.status_code==201,'conversation');cid=created.json()['id']
  person=self.people[4];second,csrf=await self.session(person['tenant'],person['person']);control=secrets.token_urlsafe(32);calls=[]
  def app_for(origin):
   self.knowledge.origin=origin;self.conversations.origin=origin
   identity=IdentityService(SimpleNamespace(public_origin=origin),PostgresIdentityRepository(self.database),None,None)
   app=create_app(Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True}),services=ApplicationServices(health=SimpleNamespace(),identity=identity,conversations=self.conversations,knowledge=self.knowledge))
   async def page():return FileResponse(ROOT/'frontend/dist/index.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
   for path in ('/','/chat','/chat/{id}','/appearance','/access','/tenants','/sources/{id}'):
    app.add_api_route(path,page,methods=['GET'],include_in_schema=False)
   async def fixture(request:Request):
    if request.headers.get('x-fixture-control')!=control:return JSONResponse({},status_code=404)
    action=request.headers.get('x-fixture-action')
    if action in ('answer','clarification','source_conflict'):
     claim=await self.scheduler.claim();require(claim.get('job_type')=='agent.resume','actual-agent-job')
     @contextmanager
     def factory(observer):
      model=Models(observer,action);yield SimpleNamespace(chat=model,embedding=model);calls.extend(model.calls)
     worker=await build_rag_turn(self.database,self.jobs,claim,model_factory=factory,mode='vector');await worker.run(build_rag_graph)
     return JSONResponse({'processed':True})
    if action=='vote-other':
     mid=str(UUID(request.headers.get('x-fixture-message','')))
     await self.conversations.feedback(second,{'origin':origin,'content-type':'application/json','x-csrf-token':csrf},mid,dict(key=str(uuid4()),rating='up',reason=None));return JSONResponse({'voted':True})
    if action=='revoke':
     for row in rows:require((await self.mutate(row,'acl',readers=[],extra_headers={**self.headers(0,'application/json'),'origin':origin})).status_code==200,'revoke')
     return JSONResponse({'revoked':True})
    return JSONResponse({},status_code=400)
   app.add_api_route('/__fixture/control',fixture,methods=['POST'],include_in_schema=False);app.mount('/assets',StaticFiles(directory=str(ROOT/'frontend/dist/assets')));return app
  proof=await run_browser(OUT,'frontend-chat-turns-browser.mjs',app_for,dict(owner=person['token'],conversation=cid,control=control),timeout=300)
  require(await self.scalar('SELECT count(*) FROM {s}.messages WHERE author_kind=\'human\'')==3,'three-human-inputs-no-replay-duplicates')
  require(await self.scalar('SELECT count(*) FROM {s}.runs')==3,'three-runs-only')
  require(await self.scalar("SELECT count(*) FROM {s}.outbox_jobs WHERE job_type='agent.resume' AND state='completed'")==3,'real-jobs-completed')
  require(await self.scalar("SELECT active_run_id IS NULL FROM {s}.conversations WHERE id=%s",(UUID(cid),)) is True,'input-unblocked')
  require(await self.scalar("SELECT count(*) FROM {s}.messages WHERE author_kind='model' AND provenance_mode='dependent'")==3,'all-three-model-messages-bind-retrieved-context')
  write_json(OUT/'graph-proof.json',dict(passed=True,human_messages=3,runs=3,completed_jobs=3,deterministic_model_calls=calls,paid_calls=0))
  self.passed('actual_graph_browser_'+str(len(proof['checks']))+'_checks');self.passed('three_turns_no_duplicate_admission_or_feedback_inference')

def main():
 logging.disable(logging.CRITICAL);p=argparse.ArgumentParser(description=__doc__);p.add_argument('--live',action='store_true');args=p.parse_args()
 if not args.live:p.error('Explicit --live required; owned existing test DB only.')
 try:
  cfg=load_settings(Path('.env'),require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(cfg.database_url.get_secret_value());direct=parse_target(cfg.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=20,operation='chat_turns'))
  result['frontend_source_sha256']=frontend_hashes();write_json(OUT/'database-proof.json',result);print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','frontend_source_sha256')}))
 except Exception as error:print('Chat turns verification failed: '+(str(error) if isinstance(error,CheckFailure) else type(error).__name__));raise SystemExit(1) from None
if __name__=='__main__':main()
