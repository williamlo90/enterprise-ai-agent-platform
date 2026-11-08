"""SEC09 delta: actual source/actor/receipt/history/checkpoint gates, no model/cloud."""
from check_frontend_workflow_checkpoint import *
from check_frontend_workflow_checkpoint import Harness as Base
from enterprise_ai_platform.infrastructure.database.checkpoints import PostgresCheckpointRepository
from enterprise_ai_platform.infrastructure.database.document_sources import DatabaseDocumentSources
from enterprise_ai_platform.integrations.checkpoints.langgraph import BoundCheckpointSaver
from enterprise_ai_platform.modules.conversations.ports import SourceRef,ConversationFailure
from enterprise_ai_platform.modules.conversations.service import DenySources
from enterprise_ai_platform.modules.identity.service import digest
from dataclasses import asdict,replace as dataclass_replace
OUT=ROOT/'local/sec09'
class Harness(Base):
 async def bridge(self):
  original=workflow_foundation.Settings
  try:
   with patch.object(workflow_foundation,'Settings',lambda **kwargs:original(**{**kwargs,'conversations_enabled':True})):await super().bridge()
  except Exception as error:
   print(json.dumps(dict(diagnostic_type=type(error).__name__,frames=[dict(file=Path(f.filename).name,function=f.name,line=f.lineno) for f in traceback.extract_tb(error.__traceback__)])),flush=True);raise
 async def run_flow(self,browser,resources,client,pins,service,execute,trip,item,policy,body,internal):
  origin_now=ORIGIN;tenant=self.people[0]['tenant'];reviewers=[]
  await self.execute("UPDATE {s}.internal_caller_credentials SET allowed_actions=allowed_actions||'[\"workflow.read\",\"trips.list\"]'::jsonb WHERE credential_hash=%s",(self.credential_hash,))
  for route in body['rules']['approval_route']:
   who=UUID(route['approver_id']);mid=await self.member(tenant,who,grants=(RoleGrant('employee','tenant',str(tenant)),RoleGrant('approver','process','reimbursement')));token,csrf=await self.session(tenant,who);reviewers.append(dict(token=token,csrf=csrf,member=mid))
  operator=uuid4();await self.member(tenant,operator,grants=(RoleGrant('operator_scoped','operations','provisioning'),RoleGrant('operator_scoped','operations','job_metadata')));operator_token,_=await self.session(tenant,operator)
  async def headers(token):
   cookie={'cookie':'__Host-eai-session='+token};me=await browser.get('/api/v1/me',headers={**cookie,'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'});require(me.status_code==200,'batch-bootstrap');return {**cookie,'origin':origin_now,'content-type':'application/json','x-csrf-token':me.json()['csrf_token'],'x-eai-expected-context':me.json()['context_binding']}
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
   content=dict(currency='SGD',items=[dict(id=str(uuid4()),trip_item_id=item,amount_minor=500,receipt_ids=[fid])],receipts=[dict(id=fid,version=file['version'])]);r=await browser.patch(path,headers=h,json=dict(expected_version=1,content=content,key=str(uuid4())));require(r.status_code==200,'batch-edit');r=await browser.post(path+'/preview',headers=h,json=dict(expected_version=2));require(r.status_code==200,'batch-preview');r=await browser.post(path+'/confirm',headers=h,json=dict(expected_version=2,material_hash=r.json()['material_hash'],key=str(uuid4())));require(r.status_code==202,'batch-confirm');await dispatch();rows=await browser.get('/api/v1/claims',headers=h);return next(v['id'] for v in rows.json()['items'] if v['state']=='pending_approval')
  cid=await seed();reviewer=reviewers[0];h=await headers(reviewer['token']);original_member=await self.scalar('SELECT to_jsonb(m) FROM {s}.memberships m WHERE id=%s',(reviewer['member'],))
  r=await browser.post('/api/v1/conversations',headers=h,json=dict(language='en',key=str(uuid4())));require(r.status_code==201,'sec-conversation-create');conversation=r.json()['id']
  r=await browser.post('/api/v1/conversations/'+conversation+'/messages',headers=h,json=dict(text='Fictional provenance fixture request.',client_message_id=str(uuid4())));require(r.status_code==202,'sec-run-create');rid=r.json()['run_id']
  picked=await self.scheduler.claim();require(picked.get('id')==rid,'sec-agent-claim');lease=Lease(UUID(rid),tenant,picked['generation'])
  live=resources.conversations.repository.service_sources;require(live is not None,'sec-composed-live-service-verifier')
  binding=dict(credential_hash=jobs._credential_hash,tenant_id=str(tenant),run_id=rid,job_id=rid,generation=picked['generation'])
  repo=PostgresCheckpointRepository(self.database,DenySources(),source_factory=lambda c:DatabaseDocumentSources(self.database,c,{**binding,'epoch':saver.binding['epoch']},history=False),service_sources=live)
  saver=BoundCheckpointSaver(repo,**binding);await saver.start(model_ref='fixture-no-inference',prompt_version='sec09',config_version=1)
  cp_id=str(uuid4());config=await saver.aput(saver.config(),dict(id=cp_id,v=1,channel_values=dict(value='Fictional private claim result'),channel_versions={},versions_seen={},updated_channels=[]),dict(source='update',step=1,parents={}),{})
  await saver.aput_writes(config,[('private_fixture','Fictional pending claim result')],'fixture-task')
  await self.execute("UPDATE {s}.outbox_jobs SET attempt_deadline=clock_timestamp()+interval '10 minutes' WHERE id=%s",(rid,)) # owned long probe fixture, production default120s unchanged
  doc=self.policy_fixture['source'];did=doc['document_id'];vid=doc['version_id'];reviewer_id=body['rules']['approval_route'][0]['approver_id'];doc_revision=await self.scalar('SELECT access_revision FROM {s}.documents WHERE id=%s',(did,))
  await self.execute('INSERT INTO {s}.document_acl(tenant_id,document_id,principal_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(tenant,did,reviewer_id))
  refs=[SourceRef('service','claim',cid,'1','active_reviewer','1'),SourceRef('service','receipt',fid,str(file['version']),'active_reviewer','1'),SourceRef('platform','document_version',vid,'1','read',str(doc_revision))]
  raw_verify=live.verify;raw_read=live.reader.read;raw_auth=live.receipt_authority.authorize
  async def diagnostic_call(name,fn,*args,**kwargs):
   try:
    value=await fn(*args,**kwargs)
    if name=='source':print(json.dumps(dict(diagnostic=name,version=value[0]['claim']['version'],revision=value[0]['claim']['revision'],current_reviewer=value[0]['claim']['reviewer_id']==args[0]['actor_id'])),flush=True)
    return value
   except Exception as error:
    print(json.dumps(dict(diagnostic=name,error_type=type(error).__name__,frames=[dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(error.__traceback__)])),flush=True);raise
  live.reader.read=lambda *a,**kw:diagnostic_call('source',raw_read,*a,**kw)
  live.receipt_authority.authorize=lambda *a,**kw:diagnostic_call('receipt',raw_auth,*a,**kw)
  async def diagnostic_verify(*args):
   await jobs.heartbeat(lease);permit=await raw_verify(*args);print(json.dumps(dict(diagnostic='permit',allowed=list(permit.allowed.values()),proof_count=len(permit.envelopes))),flush=True);return permit
  live.verify=diagnostic_verify

  mid=uuid4();sequence=await self.scalar('SELECT next_sequence FROM {s}.conversations WHERE id=%s',(conversation,))
  await self.execute("INSERT INTO {s}.messages(tenant_id,id,conversation_id,author_kind,body,sequence,run_id,output_kind,provenance_mode) VALUES(%s,%s,%s,'model','Fictional private claim result',%s,%s,'answer','dependent')",(tenant,mid,conversation,sequence,rid))
  await self.execute('UPDATE {s}.conversations SET next_sequence=next_sequence+1,version=version+1 WHERE id=%s',(conversation,))
  for ref in refs:
   for message,run in ((mid,None),(None,rid)):
    await self.execute('INSERT INTO {s}.source_dependencies(tenant_id,id,conversation_id,output_message_id,run_id,source_system,resource_type,resource_id,resource_version,required_relation,observed_revision,platform_document_version_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(tenant,uuid4(),conversation,message,run,ref.system,ref.kind,ref.resource_id,ref.version,ref.relation,ref.observed_revision,ref.resource_id if ref.system=='platform' else None))
  saver.binding['sources']=[asdict(r) for r in refs]
  async def beat():await jobs.heartbeat(lease)
  async def history(redacted=False):
   await beat();r=await browser.get('/api/v1/conversations/'+conversation+'/messages',headers=h);require(r.status_code==200,'sec-history-'+str(r.status_code));m=next(v for v in r.json()['messages'] if v['id']==str(mid))
   if redacted:require(m['redacted'] and m['text'] is None and not m['sources'] and not m['citations'] and m['feedback'] is None,'sec-complete-redaction')
   else:
    print(json.dumps(dict(diagnostic='history_shape',redacted=m['redacted'],source_count=len(m['sources']),has_text=m['text'] is not None)),flush=True)
    require(not m['redacted'] and m['text']=='Fictional private claim result' and {v['kind'] for v in m['sources']}=={'claim','receipt','document_version'},'sec-positive-current-body-and-sources')
   return m
  async def rejected(call,status=403,heartbeat=True):
   if heartbeat:await beat()
   try:await call
   except ConversationFailure as e:require(e.status==status,'sec-denial-status-'+str(e.status))
   else:raise CheckFailure('sec-unexpected-grant')
  await history();await beat();require((await saver.aget_tuple(config)).checkpoint['channel_values']['value']=='Fictional private claim result','sec-positive-checkpoint');await beat();items=[v async for v in saver.alist(saver.config())];require(len(items)==1 and len(items[0].pending_writes)==1,'sec-positive-list-and-intermediate')
  r=await browser.post('/api/v1/messages/'+str(mid)+'/feedback',headers=h,json=dict(key=str(uuid4()),rating='up',reason='Fictional feedback'));require(r.status_code==200,'sec-feedback-write');require((await history())['feedback'] is not None,'sec-positive-feedback')
  self.passed('real_current_reviewer_history_claim_bound_receipt_feedback_checkpoint_get_list_and_pending_write_visible')
  await rejected(saver.aput(config,dict(id=str(uuid4()),v=1,channel_values={},channel_versions={},versions_seen={},updated_channels=[]),{},{}));await rejected(saver.aput_writes(config,[('value','untrusted service cache')],'bad-task'))
  self.passed('unsupported_service_dependent_checkpoint_mutation_denied_before_write')
  await self.execute("UPDATE {s}.receipt_receiver_keys SET active=false WHERE credential_hash=%s",(self.credential_hash,));await history(True);await rejected(saver.aget_tuple(config));await self.execute("UPDATE {s}.receipt_receiver_keys SET active=true WHERE credential_hash=%s",(self.credential_hash,));await history()
  self.passed('actual_local_receipt_key_withdrawal_redacts_whole_derived_message_and_denies_checkpoint_then_fresh_recheck')
  original_authorize=live.receipt_authority.authorize
  async def forged(*args,**kwargs):
   access,envelope=await original_authorize(*args,**kwargs);return access,{**envelope,'proof':'0'*64}
  with patch.object(live.receipt_authority,'authorize',forged):await history(True);await rejected(saver.aget_tuple(config))
  self.passed('actual_SQL_rejects_forged_receipt_attestation_despite_current_source_relationship')
  original_read=live.reader.read
  async def outage(*args,**kwargs):raise httpx.ReadTimeout('Fixture outage')
  with patch.object(live.reader,'read',outage):await history(True);await rejected(saver.aget_tuple(config))
  async def expired(*args,**kwargs):
   projection,until=await original_read(*args,**kwargs);return projection,time.time()-1
  with patch.object(live.reader,'read',expired):
   # Expired source authority is denied, never a stale cached positive.
   await rejected(saver.aget_tuple(config),401)
  self.passed('current_source_outage_and_expired_authority_failclosed_no_cached_checkpoint_grant')
  await history()
  remote_reads=0
  async def late_file_revoke(*args,**kwargs):
   nonlocal remote_reads
   result=await original_read(*args,**kwargs);remote_reads+=1
   if remote_reads==3:await self.execute('DELETE FROM {s}.document_acl WHERE tenant_id=%s AND document_id=%s AND principal_id=%s',(tenant,did,reviewer_id))
   return result
  with patch.object(live.reader,'read',late_file_revoke):await history(True)
  await self.execute('INSERT INTO {s}.document_acl(tenant_id,document_id,principal_id) VALUES(%s,%s,%s)',(tenant,did,reviewer_id));remote_reads=0
  with patch.object(live.reader,'read',late_file_revoke):await rejected(saver.aget_tuple(config))
  await self.execute('INSERT INTO {s}.document_acl(tenant_id,document_id,principal_id) VALUES(%s,%s,%s)',(tenant,did,reviewer_id));await history()
  self.passed('mixed_document_ACL_revoked_during_final_remote_await_is_rechecked_before_history_or_checkpoint_buffer_returns')

def main():
 logging.disable(logging.CRITICAL);p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');args=p.parse_args()
 if not args.live:p.error('Explicit --live required, owned fixtures only.')
 OUT.mkdir(parents=True,exist_ok=True);report={'passed':False}
 try:
  cfg=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(cfg.database_url.get_secret_value());direct=parse_target(cfg.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct);Harness.reference_target=Target(direct.host,direct.port,direct.dbname,direct.user,direct.password,direct.channel_binding)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:report=runner.run(verify(direct,base,harness_type=Harness,target_version=30,operation='bridge',direct_timeout=600))
 except Exception as error:
  report.update(error_type=type(error).__name__,frames=[dict(file=Path(f.filename).name,function=f.name,line=f.lineno) for f in traceback.extract_tb(error.__traceback__)])
  if isinstance(error,CheckFailure):report['check']=str(error)
 report.update(paid_calls=0,scope='Actual restricted owned platform30/reference6 composed source, history and checkpoint gates. Derived rows/checkpoint payload seeded as owned fixtures; identity/storage/parser/interservice ASGI controlled, no browser/model/cloud proof.')
 (OUT/'configured-proof.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}));raise SystemExit(0 if report['passed'] else 1)
if __name__=='__main__':main()
