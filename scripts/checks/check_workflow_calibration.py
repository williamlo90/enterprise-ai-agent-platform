"""MODELSEL03B local native planner budgeting + actual shared transaction bridge."""
from check_frontend_workflow_checkpoint import *
from check_frontend_workflow_checkpoint import Harness as Base
from contextlib import contextmanager
from scripts.evaluation.workflow_calibration import prepare,RATES
from scripts.evaluation.workflow_model_port import BudgetedWorkflowPlanner
from enterprise_ai_platform.evaluation.workflow import WorkflowJournal
from enterprise_ai_platform.integrations.models.workflow import NovaWorkflowPlanner
from enterprise_ai_platform.integrations.checkpoints.assisted_graph import build_assisted_graph
OUT=ROOT/'local/model-sel-03b'
class Harness(Base):
 async def bridge(self):
  original=workflow_foundation.Settings
  with patch.object(workflow_foundation,'Settings',lambda **kwargs:original(**{**kwargs,'conversations_enabled':True})):await super().bridge()
 async def run_flow(self,browser,resources,client,pins,service,execute,trip,item,policy,body,internal):
  tenant=self.people[0]['tenant']
  await self.execute("UPDATE {s}.internal_caller_credentials SET allowed_actions=allowed_actions||'[\"workflow.read\",\"trips.list\"]'::jsonb WHERE credential_hash=%s",(self.credential_hash,))
  for stage in body['rules']['approval_route']:
   await self.member(tenant,UUID(stage['approver_id']),grants=(RoleGrant('employee','tenant',str(tenant)),RoleGrant('approver','process','reimbursement')))
  async def headers():
   h={'cookie':'__Host-eai-session='+self.people[0]['token']};r=await browser.get('/api/v1/me',headers={**h,'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'});require(r.status_code==200,'calibration-bootstrap')
   return {**h,'origin':ORIGIN,'content-type':'application/json','x-csrf-token':r.json()['csrf_token'],'x-eai-expected-context':r.json()['context_binding']}
  uploaded=await self.upload(b'%PDF-1.4\n'+b'Fictional calibration receipt '*1000,purpose='receipt',mime='application/pdf');require(uploaded.status_code==202,'calibration-file');await self.process();fid=uploaded.json()['id'];file=await self.scalar('SELECT to_jsonb(f) FROM {s}.files f WHERE id=%s',(fid,))
  secret=secrets.token_urlsafe(32);jobs=JobService(PostgresJobRepository(self.database),secret)
  await self.execute("UPDATE {s}.job_callers SET active=false WHERE tenant_id=%s AND capability='executor'",(tenant,))
  await self.execute("INSERT INTO {s}.job_callers(id,credential_hash,capability,tenant_id,expires_at) VALUES(%s,%s,'executor',%s,clock_timestamp()+interval '1 hour')",(uuid4(),jobs._credential_hash,tenant))
  _,cfg=prepare(OUT/('live-bridge-config-'+self.schema));journal=WorkflowJournal(OUT/('live-bridge-attempts-'+self.schema+'.sqlite3'),cfg);current='';calls=[]
  class SDK:
   def converse(self,**kwargs):
    data=json.loads(kwargs['messages'][0]['content'][0]['text']);require(set(data)=={'request','trip','selected_receipts'},'calibration-no-gold-in-model')
    value=dict(name='prepare_reimbursement',input=dict(currency='SGD',items=[dict(trip_item_id=item,amount_minor=500,receipt_ids=[fid])])) if current=='prepare' else dict(name='clarify_reimbursement',input=dict(reason='missing_amount'))
    calls.append(current);return dict(usage=dict(inputTokens=100,outputTokens=60,totalTokens=160),stopReason='tool_use',output=dict(message=dict(role='assistant',content=[dict(toolUse=dict(toolUseId='fixture',**value))])))
  @contextmanager
  def models(observer):yield BudgetedWorkflowPlanner(journal,'live-'+current,1,RATES,observer,lambda before:NovaWorkflowPlanner(SDK(),before))
  async def start(kind):
   nonlocal current
   current=kind;h=await headers();r=await browser.post('/api/v1/conversations',headers=h,json=dict(language='en',key=str(uuid4())));require(r.status_code==201,'calibration-conversation');cid=r.json()['id']
   selected=dict(kind='submission',trip_id=trip,claim_id=None,receipts=[dict(id=fid,version=file['version'])]);r=await browser.post('/api/v1/conversations/'+cid+'/workflow',headers=h,json=dict(text='Prepare500 SGD minor units.' if kind=='prepare' else 'Amount not known yet; ask me first.',client_message_id=str(uuid4()),selection=selected));require(r.status_code==202,'calibration-admit');rid=r.json()['run_id']
   picked=await self.scheduler.claim();require(picked.get('id')==rid,'calibration-owned-agent-job');turn=await platform_composition.build_assisted_turn(self.database,jobs,picked,source=resources.drafts.source,model_factory=models);result=await turn.run(build_assisted_graph)
   handle=await browser.get('/api/v1/runs/'+rid+'/workflow',headers=await headers());require(handle.status_code==200,'calibration-private-handle');return result,handle.json(),rid
  result,handle,rid=await start('prepare');require(result.get('__interrupt__') and handle['draft_id'] is not None,'calibration-real-human-interrupt');require(await self.scalar('SELECT count(*) FROM {s}.actions')==0,'calibration-no-model-consent')
  self.passed('new_financial_native_planner_wrapper_preserves_actual_app_reservation_original_deadline_and_shared_draft_human_pause')
  path='/api/v1/drafts/'+handle['draft_id'];h=await headers();draft=(await browser.get(path,headers=h)).json();preview=await browser.post(path+'/preview',headers=h,json=dict(expected_version=draft['version']));require(preview.status_code==200,'calibration-current-preview');r=await browser.post(path+'/confirm',headers=h,json=dict(expected_version=draft['version'],material_hash=preview.json()['material_hash'],key=str(uuid4())));require(r.status_code==202,'calibration-explicit-human-consent')
  picked=await self.scheduler.claim();require(picked.get('job_type')=='action.dispatch','calibration-action-job');worker=platform_composition.workflow_worker(self.flow_settings,self.database,secret,transport=ObservedTransport(self.routed))
  try:terminal=await worker.process(Lease(UUID(picked['id']),tenant,picked['generation']))
  finally:await worker.transport.close()
  require(terminal.get('state')=='committed','calibration-actual-flow-commit');claim=await browser.get('/api/v1/claims/'+terminal['result']['ref'],headers=await headers());require(claim.status_code==200 and claim.json()['claim']['state']=='pending_approval' and claim.json()['claim']['financial']['total_minor']==500,'calibration-current-source-outcome')
  self.passed('new_budget_bridge_model_proposal_is_not_task_success_until_explicit_human_ACT_real_FLOW_and_current_source_pending_approval')
  result,handle,_=await start('clarify');require(result['phase']=='clarification' and handle['draft_id'] is None and await self.scalar('SELECT count(*) FROM {s}.actions')==1,'calibration-clarification-no-extra-action')
  require(calls==['prepare','clarify'] and await self.scalar("SELECT count(*) FROM {s}.rag_attempts WHERE state='known'")==2,'calibration-two-known-application-attempts');attempts=journal.attempts('live-prepare',1)+journal.attempts('live-clarify',1);require(len(attempts)==2 and all(a.cost_usd is not None for a in attempts),'calibration-two-financial-attempts')
  self.passed('clarification_creates_no_draft_or_consent_and_both_app_and_evaluation_usage_ledgers_settle_two_native_fixture_calls')

def main():
 logging.disable(logging.CRITICAL);p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');a=p.parse_args()
 if not a.live:p.error('Explicit owned DB --live required; no provider inference.')
 OUT.mkdir(parents=True,exist_ok=True);report={'passed':False}
 try:
  cfg=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(cfg.database_url.get_secret_value());direct=parse_target(cfg.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct);Harness.reference_target=Target(direct.host,direct.port,direct.dbname,direct.user,direct.password,direct.channel_binding)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:report=runner.run(verify(direct,base,harness_type=Harness,target_version=30,operation='bridge',direct_timeout=600))
 except Exception as e:
  report.update(error_type=type(e).__name__,frames=[dict(file=Path(f.filename).name,function=f.name,line=f.lineno) for f in traceback.extract_tb(e.__traceback__)])
  if isinstance(e,CheckFailure):report['check']=str(e)
 report.update(provider_calls=0,model_quality_accepted=False,scope='Actual owned platform30/reference6 source/graph/API; native SDK/storage/parser/identity and ASGI IO fixtures. Two cases verify new budgeting boundary, not model quality or tenant-language benchmark.')
 (OUT/'application-proof.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}));raise SystemExit(0 if report['passed'] else 1)
if __name__=='__main__':main()
