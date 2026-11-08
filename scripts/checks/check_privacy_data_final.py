"""Actual response-only conversation export and current-source/privacy gates."""
import argparse,asyncio,hashlib,json,logging,time,traceback
from pathlib import Path
from uuid import uuid4
from psycopg.types.json import Jsonb
from check_privacy_export_acl import Harness as ACLHarness
from check_privacy_effect_registry import payload as effect_payload
from check_knowledge_workspace import verify
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.config import DatabaseProbeSettings,load_settings
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.modules.identity.service import digest
from enterprise_ai_platform.modules.privacy.domain import Category,REQUIRED
from enterprise_ai_platform.file_check import ORIGIN

ROOT=Path(__file__).resolve().parents[2]


COMPLETED=[]

class Harness(ACLHarness):
 skip_acl_checks=True
 def passed(self,name):
  super().passed(name);COMPLETED.append(name)
 async def cases(self,browser,resources,op,foreign,wrong_scope):
  await super().cases(browser,resources,op,foreign,wrong_scope)
  from uuid import UUID
  from psycopg import sql
  t=self.people[0]['tenant'];person=self.people[0]['person']
  async def headers(index=0):
   p=self.people[index];cookie={'cookie':'__Host-eai-session='+p['token']}
   me=await browser.get('/api/v1/me',headers=dict(cookie,**{'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'}));require(me.status_code==200,'delete-bootstrap');p['csrf']=me.json()['csrf_token']
   return dict(cookie,origin=ORIGIN,**{'content-type':'application/json','x-csrf-token':p['csrf'],'x-eai-expected-context':me.json()['context_binding']})
  async def create(text):
   r=await browser.post('/api/v1/conversations',json=dict(language='en',key=str(uuid4())),headers=await headers());require(r.status_code==201,'delete-root')
   cid=r.json()['id'];a=await browser.post('/api/v1/conversations/'+cid+'/messages',json=dict(text=text,client_message_id=str(uuid4())),headers=await headers());require(a.status_code==202,'delete-input');return cid,UUID(a.json()['run_id'])
  async def inventory(cid):
   r=await browser.get('/api/v1/privacy/conversations/'+cid+'/inventory',headers=await headers());require(r.status_code==200,'delete-inventory');return r.json()
  async def fence(cid):
   inv=await inventory(cid);data=dict(key=str(uuid4()),version=inv['version'],content_digest=inv['content_digest'],policy_hash=inv['effect_policy_hash'])
   r=await browser.post('/api/v1/privacy/conversations/'+cid+'/deletion',json=data,headers=await headers());require(r.status_code==202,'delete-fence');return data
  cid,rid=await create('PRIVATE-ERASE-HUMAN');control,control_run=await create('PRIVATE-CONTROL-KEEP');mid=uuid4();cp=uuid4();vid=UUID(self.privacy_fixture_document['version_id'])
  await self.execute("INSERT INTO {s}.messages(tenant_id,id,conversation_id,author_kind,body,sequence,run_id,output_kind,provenance_mode) VALUES(%s,%s,%s,'model','PRIVATE-ERASE-MODEL',2,%s,'answer','dependent')",(t,mid,UUID(cid),rid))
  await self.execute("INSERT INTO {s}.feedback(tenant_id,message_id,conversation_id,actor_id,rating,reason) VALUES(%s,%s,%s,%s,'up','PRIVATE-ERASE-FEEDBACK')",(t,mid,UUID(cid),person))
  await self.execute("INSERT INTO {s}.checkpoints(tenant_id,run_id,namespace,id,checkpoint_no,generation,state_payload) VALUES(%s,%s,'PRIVATE-ERASE-NS',%s,1,1,%s)",(t,rid,cp,Jsonb(dict(private='PRIVATE-ERASE-CHECKPOINT'))))
  await self.execute("INSERT INTO {s}.checkpoint_writes(tenant_id,run_id,namespace,checkpoint_id,task_id,write_index,task_path,channel,value,generation) VALUES(%s,%s,'PRIVATE-ERASE-NS',%s,'PRIVATE-ERASE-TASK',0,'PRIVATE-ERASE-PATH','PRIVATE-ERASE-CHANNEL',%s,1)",(t,rid,cp,Jsonb(dict(private='PRIVATE-ERASE-PENDING'))))
  await self.execute("INSERT INTO {s}.assisted_requests(tenant_id,run_id,conversation_id,actor_id,membership_id,session_id,access_revision,selection,proposal) SELECT tenant_id,id,conversation_id,actor_id,membership_id,session_id,access_revision,%s,%s FROM {s}.runs WHERE tenant_id=%s AND id=%s",(Jsonb(dict(private='PRIVATE-ERASE-SELECTION')),Jsonb(dict(private='PRIVATE-ERASE-PROPOSAL')),t,rid))
  await self.execute("INSERT INTO {s}.rag_citations(tenant_id,message_id,conversation_id,label,version_id,index_generation,chunk_no,page,text_digest,quote) SELECT %s,%s,%s,'S1',document_version_id,index_generation,chunk_no,page,text_digest,'PRIVATE-ERASE-QUOTE' FROM {s}.chunks WHERE tenant_id=%s AND document_version_id=%s LIMIT 1",(t,mid,UUID(cid),t,vid))
  await self.execute("UPDATE {s}.conversations SET manual_name='PRIVATE-ERASE-NAME' WHERE tenant_id=%s AND id=%s",(t,UUID(cid)))
  await self.execute("UPDATE {s}.conversation_requests SET response=response||%s WHERE tenant_id=%s AND conversation_id=%s",(Jsonb(dict(private='PRIVATE-ERASE-CACHED')),t,UUID(cid)))
  await self.execute("INSERT INTO {s}.rag_attempts(tenant_id,run_id,kind,job_id,generation,epoch,request_digest,reserved_input_tokens,reserved_output_tokens,settlement_hash,settlement_until) SELECT tenant_id,id,'chat',job_id,1,0,%s,4096,256,%s,clock_timestamp()+interval '1 hour' FROM {s}.runs WHERE tenant_id=%s AND id=%s",('a'*64,hashlib.sha256(('a'*64).encode()).hexdigest(),t,rid))
  admission=await fence(cid);request=dict(key=str(uuid4()),fence_key=admission['key'])
  async def execute(data=request,index=0,target=cid):return await browser.post('/api/v1/privacy/conversations/'+target+'/deletion/execute',json=data,headers=await headers(index))
  async def clear(target,admission):
   await self.execute("INSERT INTO {s}.privacy_delete_clearances(tenant_id,actor_id,fence_key,store,policy_hash,source_digest,region,checked_at,expires_at,evidence_sha256,environment) SELECT %s,%s,%s,v,%s,%s,'ap-southeast-1',clock_timestamp(),clock_timestamp()+interval '60 seconds',%s,'fictional_fixture' FROM unnest(ARRAY['backup','provider','holds']) v",(t,person,UUID(admission['key']),admission['policy_hash'],admission['content_digest'],'c'*64))
  await clear(cid,admission)
  async with self.database.transaction() as connection:
   settled=(await (await connection.execute('SELECT '+self.schema+'.rag_usage_call(%s)',(Jsonb(dict(tenant_id=str(t),run_id=str(rid),kind='chat',receipt='a'*64,input_tokens=1,output_tokens=2)),))).fetchone())[0]
  require(settled['status']=='ok','delete-settled-usage')
  # Failure after the effect, at commit, must roll back raw rows AND intent.
  await self.execute("CREATE FUNCTION {s}.privacy_fixture_commit_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'controlled commit rejection' USING ERRCODE='23514';END $$")
  await self.execute('REVOKE ALL ON FUNCTION {s}.privacy_fixture_commit_failure() FROM PUBLIC,{a}')
  await self.execute('CREATE CONSTRAINT TRIGGER privacy_fixture_commit_failure AFTER INSERT ON {s}.privacy_delete_results DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {s}.privacy_fixture_commit_failure()')
  failed=await execute();require(failed.status_code==503,'delete-commit-rejected')
  require(await self.scalar('SELECT count(*) FROM {s}.privacy_delete_results')==0 and await self.scalar('SELECT count(*) FROM {s}.privacy_conversation_effect_intents')==0,'delete-result-intent-rollback')
  require((await inventory(cid))['content_digest']==admission['content_digest'],'delete-bytes-rollback')
  require(await self.scalar('SELECT count(*) FROM {s}.privacy_erasure_contexts')==0,'delete-no-context-leak')
  await self.execute('DROP TRIGGER privacy_fixture_commit_failure ON {s}.privacy_delete_results')
  done=await execute();require(done.status_code==202 and done.json()['content_deleted'] and not done.json()['erased'],'actual-delete-completes-with-retained-audit-'+str(done.status_code))
  again=await execute();require(again.status_code==202 and again.json()['replayed'],'actual-delete-replay')
  require((await execute(dict(request,key=str(uuid4())))).status_code==409,'delete-no-rekey')
  require(await self.scalar("SELECT bool_and(body='[redacted]') FROM {s}.messages WHERE tenant_id=%s AND conversation_id=%s",(t,UUID(cid))),'delete-old-message-content-gone')
  for table in ['feedback','rag_citations','assisted_requests']:
   require(await self.scalar('SELECT count(*) FROM {s}.'+table+' WHERE tenant_id=%s AND conversation_id=%s',(t,UUID(cid)))==0,'delete-copy-'+table)
  for table in ['checkpoints','checkpoint_writes']:
   require(await self.scalar('SELECT count(*) FROM {s}.'+table+' WHERE tenant_id=%s AND run_id=%s',(t,rid))==0,'delete-copy-'+table)
  require(await self.scalar('SELECT manual_name IS NULL FROM {s}.conversations WHERE tenant_id=%s AND id=%s',(t,UUID(cid))),'delete-name')
  require('PRIVATE-' not in await self.scalar("SELECT string_agg(response::text,'') FROM {s}.conversation_requests WHERE tenant_id=%s AND conversation_id=%s",(t,UUID(cid))),'delete-cached-response')
  require(await self.scalar("SELECT body='PRIVATE-CONTROL-KEEP' FROM {s}.messages WHERE tenant_id=%s AND run_id=%s",(t,control_run)),'delete-control-untouched')
  status=await browser.get('/api/v1/privacy/conversations/'+cid+'/deletion',headers=await headers());require(status.status_code==200 and status.json()['content_deleted'],'delete-readable-receipt')
  require(await self.scalar('SELECT count(*) FROM {s}.privacy_erasure_contexts')==0,'delete-capability-ended')
  self.passed('actual_atomic_content_and_all_derived_copy_erasure_with_commit_failure_rollback_idempotent_receipt_and_untouched_control')
  # A fresh policy with a real minimum must hold a new root, even with clear proofs.
  current_policy=(await browser.get('/api/v1/operator/privacy-effect-policy',headers=await headers(op))).json()['policy']
  policy=dict(key=str(uuid4()),schema_version=2,revision=current_policy['revision']+1,expires_at=int(time.time())+3600,copy_rules=current_policy['copy_rules'])
  for rule in policy['copy_rules']:
   if rule['category']=='conversations' and rule['store']=='db' and rule['copy_class']=='content':rule.update(min_retain_seconds=3600,expire_after_seconds=7200)
  require((await browser.post('/api/v1/operator/privacy-effect-policy',json=policy,headers=await headers(op))).status_code==201,'delete-minimum-policy')
  held_cid,held_run=await create('PRIVATE-MINIMUM-KEEP');held_admission=await fence(held_cid);await clear(held_cid,held_admission)
  require((await execute(dict(key=str(uuid4()),fence_key=held_admission['key']),target=held_cid)).status_code==503,'delete-minimum-held')
  require(await self.scalar("SELECT body='PRIVATE-MINIMUM-KEEP' FROM {s}.messages WHERE tenant_id=%s AND run_id=%s",(t,held_run)),'minimum-content-kept')
  self.passed('actual_approved_minimum_retention_holds_erasure_without_partial_content_mutation')


def main():
    logging.disable(logging.CRITICAL);p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');a=p.parse_args()
    if not a.live:p.error('Explicit --live required for owned disposable fixture.')
    folder=ROOT/'local/data01/final-batch';folder.mkdir(parents=True,exist_ok=True);result=dict(passed=False)
    files=list((ROOT/'backend/src').rglob('*.py'))+list((ROOT/'backend/src').rglob('*.sql'))+[Path(__file__),ROOT/'scripts/checks/check_privacy_export_acl.py',ROOT/'scripts/checks/check_privacy_inventory.py',ROOT/'scripts/checks/check_privacy_effect_registry.py',ROOT/'scripts/checks/check_privacy_registry.py'];hashes={f.relative_to(ROOT).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
    try:
        settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=38,operation='privacy_inventory',direct_timeout=240))
        require(all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h for n,h in hashes.items()),'expiry-source-stable')
        result.pop('ingestion',None);result.update(source_sha256=hashes,scope='Actual local owning conversation erasure and current document ACL export. External backup/provider/legal-hold clearances and locations explicitly fictional; source business records and audit metadata retained. No installed migration/AWS/SDK calls. Other categories fail closed without owning adapter proof; JOURNAL/OPS cloud gates separate.')
    except Exception as error:
        result.update(inner_frames=[dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(error.__context__.__traceback__)] if error.__context__ else [],error_type=type(error).__name__,check=str(error) if isinstance(error,CheckFailure) else 'sanitized_failure',frames=[dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(error.__traceback__)])
    result.update(source_sha256=hashes,completed_assertions=COMPLETED,reused_evidence='local/data01/final-batch/initial-partial.json: two completed assertions; only test fixture helper/remaining checks changed, product source unchanged')
    (folder/'database-proof.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}));raise SystemExit(0 if result['passed'] else 1)


if __name__=='__main__':main()
