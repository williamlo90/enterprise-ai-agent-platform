"""FE-CHAT-03 owned HTTPS/API/DB recovery; deterministic model, no AWS calls."""
import argparse,asyncio,json,logging,secrets
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID,uuid4
from fastapi import Request
from starlette.responses import FileResponse,JSONResponse
from starlette.staticfiles import StaticFiles
from check_frontend_chat_turns import Harness as ChatHarness
from check_knowledge_workspace import verify
from frontend_browser_fixture import run_browser
from check_frontend_organization import ROOT,frontend_hashes
from enterprise_ai_platform.rag_check import Models
from enterprise_ai_platform.conversation_check import Harness as ConversationHarness
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
OUT=ROOT/'local/fe-chat-03'

class Harness(ChatHarness):
 async def recovery_browser(self):
  self.conversations=ConversationService(PostgresConversationRepository(self.database,live_sources=True),ORIGIN)
  doc=await self.document('Travel claims must be submitted within seven days under fictional policy.');require((await self.mutate(doc,'publish')).status_code==200,'publish')
  cids=[]
  for _ in range(2):
   response=await self.create_conversation(index=4,language='en');require(response.status_code==201,'create');cids.append(response.json()['id'])
  # Older persisted turns exercise real pagination on reconnect. Their model
  # messages are explicit archived fixtures, not worker/model executions.
  for index in range(10):
   response=await self.append(cids[0],index=4,text='Archived fixture question '+str(index));require(response.status_code==202,'archive-admission')
   await ConversationHarness.model(self,response.json(),body='Archived fixture answer '+str(index),mode='source_free')
  control=secrets.token_urlsafe(32);calls=[];held={}
  def app_for(origin):
   self.knowledge.origin=origin;self.conversations.origin=origin
   identity=IdentityService(SimpleNamespace(public_origin=origin),PostgresIdentityRepository(self.database),None,None)
   app=create_app(Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True}),services=ApplicationServices(health=SimpleNamespace(),identity=identity,conversations=self.conversations,knowledge=self.knowledge))
   async def page():return FileResponse(ROOT/'frontend/dist/index.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
   for path in ('/','/chat','/chat/{id}','/appearance','/access','/tenants','/sources/{id}'):app.add_api_route(path,page,methods=['GET'],include_in_schema=False)
   async def fixture(request:Request):
    if request.headers.get('x-fixture-control')!=control:return JSONResponse({},status_code=404)
    action=request.headers.get('x-fixture-action')
    @contextmanager
    def factory(observer):
     model=Models(observer,'answer');yield SimpleNamespace(chat=model,embedding=model);calls.extend(model.calls)
    if action in ('finish','hold'):
     claim=await self.scheduler.claim();require(claim.get('job_type')=='agent.resume','actual-agent-job')
     worker=await build_rag_turn(self.database,self.jobs,claim,model_factory=factory,mode='vector')
     if action=='finish':await worker.run(build_rag_graph)
     else:
      await worker.repository.runtime('begin',{})
      reservation=await worker.repository.call('reserve',dict(kind='embedding',items=[],request_digest='a'*64))
      await worker.repository.settle('embedding',reservation['settlement_receipt'],None)
      held['job']=UUID(claim['id']);held['run']=UUID(worker.repository.binding['run_id']);write_json(OUT/'live-lease-fixture.json',dict(scope='actual claimed worker runtime/reservation without executing model; not held SQL operation',state=await self.scalar("SELECT jsonb_build_object('run_state',r.state,'job_state',j.state,'lease_future',j.lease_until>clock_timestamp()) FROM {s}.runs r JOIN {s}.outbox_jobs j ON j.id=r.job_id AND j.tenant_id=r.tenant_id WHERE r.id=%s",(held['run'],))))
     return JSONResponse({'applied':True})
    if action=='expire':
     await self.execute("UPDATE {s}.outbox_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(held['job'],));write_json(OUT/'expired-lease-fixture.json',dict(scope='owned fixture job lease deliberately expired; no provider call',state=await self.scalar("SELECT jsonb_build_object('run_state',r.state,'job_state',j.state,'lease_future',j.lease_until>clock_timestamp()) FROM {s}.runs r JOIN {s}.outbox_jobs j ON j.id=r.job_id AND j.tenant_id=r.tenant_id WHERE r.id=%s",(held['run'],))));return JSONResponse({'applied':True})
    if action=='revoke':
     require((await self.mutate(doc,'acl',readers=[],extra_headers={**self.headers(0,'application/json'),'origin':origin})).status_code==200,'revoke');return JSONResponse({'applied':True})
    if action=='invariant':
     return JSONResponse({'human':await self.scalar("SELECT count(*) FROM {s}.messages WHERE author_kind='human'"),'runs':await self.scalar('SELECT count(*) FROM {s}.runs'),'feedback':await self.scalar('SELECT count(*) FROM {s}.feedback'),'recovered':await self.scalar('SELECT count(*) FROM {s}.runs WHERE recovered_at IS NOT NULL')})
    return JSONResponse({},status_code=400)
   app.add_api_route('/__fixture/control',fixture,methods=['POST'],include_in_schema=False);app.mount('/assets',StaticFiles(directory=str(ROOT/'frontend/dist/assets')));return app
  proof=await run_browser(OUT,'frontend-chat-recovery-browser.mjs',app_for,dict(owner=self.people[4]['token'],colleague=self.people[1]['token'],foreign=self.people[2]['token'],conversation=cids[0],stalled=cids[1],control=control),timeout=360)
  values={'human_messages':await self.scalar("SELECT count(*) FROM {s}.messages WHERE author_kind='human'"),'model_messages':await self.scalar("SELECT count(*) FROM {s}.messages WHERE author_kind='model'"),'runs':await self.scalar('SELECT count(*) FROM {s}.runs'),'jobs':await self.scalar("SELECT count(*) FROM {s}.outbox_jobs WHERE job_type='agent.resume'"),'recovered':await self.scalar('SELECT count(*) FROM {s}.runs WHERE recovered_at IS NOT NULL'),'active':await self.scalar('SELECT count(*) FROM {s}.conversations WHERE active_run_id IS NOT NULL')}
  require(values==dict(human_messages=12,model_messages=11,runs=12,jobs=12,recovered=1,active=0),'exact-result-and-recovery-counts')
  require(calls==['embedding','chat'],'no-extra-model-after-reconnect-or-recover')
  write_json(OUT/'graph-proof.json',dict(passed=True,**values,deterministic_model_calls=calls,archived_fixture_turns=10,current_worker_outputs=1,paid_calls=0))
  self.passed('actual_reconnect_provenance_and_recovery_'+str(len(proof['checks']))+'_browser_checks');self.passed('one_output_one_recovery_no_replay_no_extra_job')

def main():
 logging.disable(logging.CRITICAL);p=argparse.ArgumentParser(description=__doc__);p.add_argument('--live',action='store_true');args=p.parse_args()
 if not args.live:p.error('Explicit --live required; owned existing test DB only.')
 try:
  cfg=load_settings(Path('.env'),require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(cfg.database_url.get_secret_value());direct=parse_target(cfg.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=20,operation='recovery_browser'))
  result['frontend_source_sha256']=frontend_hashes();write_json(OUT/'database-proof.json',result);print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','frontend_source_sha256')}))
 except Exception as error:print('Chat recovery verification failed: '+(str(error) if isinstance(error,CheckFailure) else type(error).__name__));raise SystemExit(1) from None
if __name__=='__main__':main()
