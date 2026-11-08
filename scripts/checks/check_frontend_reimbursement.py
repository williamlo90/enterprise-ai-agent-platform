"""FE-WF01 actual browser/shared DRAFT-ACT-FLOW on disposable owned DB."""
from check_browser_workflow import *
from check_browser_workflow import Harness as Foundation
from frontend_browser_fixture import run_browser
from starlette.responses import FileResponse,JSONResponse
from starlette.staticfiles import StaticFiles
from fastapi import Request
from starlette.applications import Starlette
from starlette.routing import Mount
OUT=ROOT/'local/fe-wf-01'
class Harness(Foundation):
 async def run_flow(self,browser,resources,client,pins,service,execute,trip,item,policy,body,internal):
  tenant=self.people[0]['tenant'];control=secrets.token_urlsafe(32);reviewers=[]
  await self.execute("UPDATE {s}.internal_caller_credentials SET allowed_actions=allowed_actions||'[\"workflow.read\",\"trips.list\"]'::jsonb WHERE credential_hash=%s",(self.credential_hash,))
  for route in body['rules']['approval_route']:
   who=UUID(route['approver_id']);member=await self.member(tenant,who,grants=(RoleGrant('employee','tenant',str(tenant)),RoleGrant('approver','process','reimbursement')));token,csrf=await self.session(tenant,who);reviewers.append(dict(token=token,csrf=csrf))
  secret=secrets.token_urlsafe(32);jobs=JobService(PostgresJobRepository(self.database),secret)
  async def dispatch():
   await self.execute("UPDATE {s}.job_callers SET active=false WHERE tenant_id=%s AND capability='executor'",(tenant,))
   await self.execute("INSERT INTO {s}.job_callers(id,credential_hash,capability,tenant_id,expires_at) VALUES(%s,%s,'executor',%s,clock_timestamp()+interval '1 hour') ON CONFLICT(credential_hash) DO UPDATE SET active=true",(uuid4(),jobs._credential_hash,tenant))
   picked=await self.scheduler.claim();require(picked.get('job_type')=='action.dispatch','actual-dispatch-job')
   w=platform_composition.workflow_worker(self.flow_settings,self.database,secret,transport=ObservedTransport(self.routed))
   try:value=await w.process(Lease(UUID(picked['id']),UUID(picked['tenant_id']),picked['generation']))
   finally:await w.transport.close()
   require(value.get('state')=='committed','actual-UI-flow-commit');return value
  public=browser._transport.app
  def app_for(origin):
   for name in ('drafts','actions','files','knowledge','organization'):
    resource=getattr(resources,name,None)
    if resource is not None and hasattr(resource,'origin'):resource.origin=origin
   if resources.identity is not None:resources.identity.provider=SimpleNamespace(public_origin=origin)
   async def page():return FileResponse(ROOT/'frontend/dist/index.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
   for path in ('/','/reimbursements','/reimbursements/drafts/{id}','/reimbursements/actions/{id}','/reimbursements/claims/{id}','/chat','/tenants','/access','/appearance'):public.add_api_route(path,page,methods=['GET'],include_in_schema=False)
   async def fixture(request:Request):
    if request.headers.get('x-fixture-control')!=control:return JSONResponse({},status_code=404)
    kind=request.headers.get('x-fixture-action')
    if kind=='scan':
     await self.process();return JSONResponse({'processed':True})
    if kind=='dispatch':
     await dispatch();return JSONResponse({'processed':True})
    if kind=='needs-information':
     owner=self.people[0];me=await browser.get('/api/v1/me',headers={'cookie':'__Host-eai-session='+owner['token'],'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'});h={'cookie':'__Host-eai-session='+owner['token'],'x-eai-expected-context':me.json()['context_binding']}
     rows=await browser.get('/api/v1/claims',headers=h);cid=rows.json()['items'][0]['id'];details=await browser.get('/api/v1/claims/'+cid,headers=h);d=details.json()['claim'];reviewer=reviewers[0]
     me=await browser.get('/api/v1/me',headers={'cookie':'__Host-eai-session='+reviewer['token'],'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'});h={'cookie':'__Host-eai-session='+reviewer['token'],'origin':origin,'content-type':'application/json','x-csrf-token':me.json()['csrf_token'],'x-eai-expected-context':me.json()['context_binding']}
     result=await browser.post('/api/v1/claims/'+cid+'/decisions',headers=h,json=dict(expected_version=d['version'],revision=d['revision'],stage=d['active_stage'],decision='request_information',reason='Mohon koreksi nominal menjadi 6.00 sesuai bukti.',key=str(uuid4())));require(result.status_code==202,'actual-needsinfo-decision');await dispatch();return JSONResponse({'claim_id':cid})
    return JSONResponse({},status_code=400)
   public.add_api_route('/__fixture/control',fixture,methods=['POST'],include_in_schema=False);public.mount('/assets',StaticFiles(directory=str(ROOT/'frontend/dist/assets')));return Starlette(routes=[Mount('/',app=public)])
  proof=await run_browser(OUT,'frontend-reimbursement-browser.mjs',app_for,dict(owner=self.people[0]['token'],foreign=self.people[1]['token'],trip=trip,item=item,control=control),timeout=360)
  require(await self.scalar('SELECT count(*) FROM {s}.drafts')==2,'two-shared-drafts-only')
  require(await self.scalar('SELECT count(*) FROM {s}.actions')==3,'exactly-submission-information-and-decision')
  self.passed('actual_UI_manual_receipt_preview_reconfirm_lost202_lookup_needsinfo_'+str(len(proof['checks']))+'_browser_checks')
def main():
 logging.disable(logging.CRITICAL);p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');args=p.parse_args()
 if not args.live:p.error('Explicit --live required, owned fixtures only.')
 OUT.mkdir(parents=True,exist_ok=True);report={'passed':False}
 try:
  cfg=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(cfg.database_url.get_secret_value());direct=parse_target(cfg.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct);Harness.reference_target=Target(direct.host,direct.port,direct.dbname,direct.user,direct.password,direct.channel_binding)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:report=runner.run(verify(direct,base,harness_type=Harness,target_version=29,operation='bridge',direct_timeout=600))
 except Exception as error:
  report.update(error_type=type(error).__name__,frames=[dict(file=Path(f.filename).name,function=f.name,line=f.lineno) for f in traceback.extract_tb(error.__traceback__)])
  if isinstance(error,CheckFailure):report['check']=str(error)
 report.update(paid_calls=0,scope='Actual React/HTTPS/session/G13/platform29/reference6/DRAFT/ACT/FLOW. Storage/parser and interservice ASGI are controlled fixtures.')
 (OUT/'configured-proof.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report));raise SystemExit(0 if report['passed'] else 1)
if __name__=='__main__':main()
