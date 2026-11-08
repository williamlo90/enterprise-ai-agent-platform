"""One combined FE-WF03/04/ADMIN02 actual browser proof, owned fixtures only."""
from check_frontend_reimbursement import *
import check_workflow_configured as workflow_foundation
from frontend_workflow_admin_fixture import run_browser
OUT=ROOT/'local/fe-workflow-admin'
class Harness(Foundation):
 async def bridge(self):
  original=workflow_foundation.Settings
  with patch.object(workflow_foundation,'Settings',lambda **kwargs:original(**{**kwargs,'administration_enabled':True,'organization_enabled':True})):await super().bridge()
 async def run_flow(self,browser,resources,client,pins,service,execute,trip,item,policy,body,internal):
  tenant=self.people[0]['tenant'];control=secrets.token_urlsafe(32);reviewers=[]
  await self.execute("UPDATE {s}.internal_caller_credentials SET allowed_actions=allowed_actions||'[\"workflow.read\",\"trips.list\"]'::jsonb WHERE credential_hash=%s",(self.credential_hash,))
  for route in body['rules']['approval_route']:
   who=UUID(route['approver_id']);mid=await self.member(tenant,who,grants=(RoleGrant('employee','tenant',str(tenant)),RoleGrant('approver','process','reimbursement')));token,csrf=await self.session(tenant,who);reviewers.append(dict(token=token,csrf=csrf))
  operator=uuid4();await self.member(tenant,operator,grants=(RoleGrant('operator_scoped','operations','provisioning'),RoleGrant('operator_scoped','operations','job_metadata')));operator_token,_=await self.session(tenant,operator)
  async def headers(token):
   cookie={'cookie':'__Host-eai-session='+token};me=await browser.get('/api/v1/me',headers={**cookie,'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'});require(me.status_code==200,'batch-bootstrap');return {**cookie,'origin':ORIGIN,'content-type':'application/json','x-csrf-token':me.json()['csrf_token'],'x-eai-expected-context':me.json()['context_binding']}
  uploaded=await self.upload(b'%PDF-1.4\n'+b'Fictional batch receipt '*1000,purpose='receipt',mime='application/pdf');require(uploaded.status_code==202,'batch-upload');await self.process();fid=uploaded.json()['id'];file=await self.scalar('SELECT to_jsonb(f) FROM {s}.files f WHERE id=%s',(fid,))
  secret=secrets.token_urlsafe(32);jobs=JobService(PostgresJobRepository(self.database),secret)
  await self.execute("UPDATE {s}.job_callers SET active=false WHERE tenant_id=%s AND capability='executor'",(tenant,))
  await self.execute("INSERT INTO {s}.job_callers(id,credential_hash,capability,tenant_id,expires_at) VALUES(%s,%s,'executor',%s,clock_timestamp()+interval '1 hour')",(uuid4(),jobs._credential_hash,tenant))
  async def dispatch():
   picked=await self.scheduler.claim();require(picked.get('job_type')=='action.dispatch','batch-dispatch-job');w=platform_composition.workflow_worker(self.flow_settings,self.database,secret,transport=ObservedTransport(self.routed))
   try:value=await w.process(Lease(UUID(picked['id']),UUID(picked['tenant_id']),picked['generation']))
   finally:await w.transport.close()
   require(value.get('state')=='committed','batch-worker-commit');return value
  async def seed():
   h=await headers(self.people[0]['token']);r=await browser.post('/api/v1/drafts',headers=h,json=dict(kind='submission',trip_id=trip,claim_id=None,conversation_id=None,key=str(uuid4())));require(r.status_code==200,'batch-draft');did=r.json()['id'];path='/api/v1/drafts/'+did
   content=dict(currency='SGD',items=[dict(id=str(uuid4()),trip_item_id=item,amount_minor=500,receipt_ids=[fid])],receipts=[dict(id=fid,version=file['version'])]);r=await browser.patch(path,headers=h,json=dict(expected_version=1,content=content,key=str(uuid4())));require(r.status_code==200,'batch-edit');r=await browser.post(path+'/preview',headers=h,json=dict(expected_version=2));require(r.status_code==200,'batch-preview');r=await browser.post(path+'/confirm',headers=h,json=dict(expected_version=2,material_hash=r.json()['material_hash'],key=str(uuid4())));require(r.status_code==202,'batch-confirm');await dispatch();rows=await browser.get('/api/v1/claims',headers=h);return rows.json()['items'][0]['id']
  cid=await seed();public=browser._transport.app
  def app_for(origin):
   for name in ('drafts','actions','files','knowledge','organization','administration','policies'):
    resource=getattr(resources,name,None)
    if resource is not None and hasattr(resource,'origin'):resource.origin=origin
   resources.identity.provider=SimpleNamespace(public_origin=origin)
   async def page():return FileResponse(ROOT/'frontend/dist/index.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
   for path in ('/','/reimbursements','/reimbursements/drafts/{id}','/reimbursements/actions/{id}','/reimbursements/claims/{id}','/chat','/tenants','/access','/appearance','/tasks','/approvals','/approvals/{id}','/admin','/admin/{path:path}','/operator','/operator/{path:path}') :public.add_api_route(path,page,methods=['GET'],include_in_schema=False)
   async def fixture(request:Request):
    if request.headers.get('x-fixture-control')!=control:return JSONResponse({},status_code=404)
    kind=request.headers.get('x-fixture-action')
    if kind=='dispatch':await dispatch();return JSONResponse({'processed':True})
    return JSONResponse({},status_code=400)
   public.add_api_route('/__fixture/control',fixture,methods=['POST'],include_in_schema=False);public.mount('/assets',StaticFiles(directory=str(ROOT/'frontend/dist/assets')));return Starlette(routes=[Mount('/',app=public)])
  proof=await run_browser(OUT,'frontend-workflow-admin-browser.mjs',app_for,dict(owner=self.people[0]['token'],admin=self.people[3]['token'],reviewers=[p['token'] for p in reviewers],operator=operator_token,claim=cid,file=fid,control=control,member=str(self.people[1]['member']),initial_admin=str(self.people[2]['person']),policy=str(policy['id'])),timeout=480)
  require(await self.scalar('SELECT count(*) FROM {s}.actions')==5,'batch-exact-five-actions');require(await self.scalar('SELECT config_version FROM {s}.tenants WHERE id=%s',(tenant,))==2,'batch-config-version2')
  self.passed('combined_actual_React_current_approval_information_revision_two_stage_owner_tasks_config_membership_policy_operator_'+str(len(proof['checks']))+'_executions')

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
 report.update(paid_calls=0,scope='Actual built React/session/G13/restricted owned platform29/reference6. Identity, storage/parser and interservice ASGI are controlled fixtures; no model/cloud proof.')
 (OUT/'configured-proof.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}));raise SystemExit(0 if report['passed'] else 1)
if __name__=='__main__':main()
