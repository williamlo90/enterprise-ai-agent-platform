"""Actual owner conversation growth fence, no erase or export claim."""
import argparse,asyncio,hashlib,json,logging,time,traceback
from pathlib import Path
from uuid import uuid4
from psycopg.types.json import Jsonb
from check_privacy_inventory import Harness as InventoryHarness
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


class Harness(InventoryHarness):
 async def cases(self,browser,resources,op,foreign,wrong_scope):
  from psycopg.errors import CheckViolation
  from psycopg import sql
  from uuid import UUID
  async def headers(index=0):
   p=self.people[index];cookie={'cookie':'__Host-eai-session='+p['token']}
   me=await browser.get('/api/v1/me',headers=dict(cookie,**{'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'}));require(me.status_code==200,'fence-bootstrap');p['csrf']=me.json()['csrf_token']
   return dict(cookie,origin=ORIGIN,**{'content-type':'application/json','x-csrf-token':p['csrf'],'x-eai-expected-context':me.json()['context_binding']})
  async def create(index=0):
   r=await browser.post('/api/v1/conversations',json=dict(language='en',key=str(uuid4())),headers=await headers(index));require(r.status_code==201,'fence-create');return r.json()['id']
  async def inventory(cid):
   r=await browser.get('/api/v1/privacy/conversations/'+cid+'/inventory',headers=await headers());require(r.status_code==200,'fence-inventory');return r.json()
  async def post(cid,data,index=0):return await browser.post('/api/v1/privacy/conversations/'+cid+'/deletion',json=data,headers=await headers(index))
  async def status(cid,index=0):return await browser.get('/api/v1/privacy/conversations/'+cid+'/deletion',headers=await headers(index))
  cid=await create();control=await create();other=await create(4);t=self.people[0]['tenant']
  legacy=dict(key=str(uuid4()),revision=1,expires_at=int(time.time())+3600,rules=[dict(category=c.value,retention_seconds=10,regions=['ap-southeast-1'],stores=sorted(REQUIRED[c]),provider_retention_seconds=0,training_allowed=False) for c in Category])
  require((await browser.post('/api/v1/operator/privacy-policy',json=legacy,headers=await headers(op))).status_code==201,'fence-legacy-fixture')
  require((await browser.post('/api/v1/operator/privacy-effect-policy',json=effect_payload(),headers=await headers(op))).status_code==201,'fence-effect-fixture')
  appended=await browser.post('/api/v1/conversations/'+cid+'/messages',json=dict(text='PRIVATE-CONTENT',client_message_id=str(uuid4())),headers=await headers());require(appended.status_code==202,'fence-human-admission');rid=UUID(appended.json()['run_id'])
  cp=uuid4();await self.execute("INSERT INTO {s}.checkpoints(tenant_id,run_id,namespace,id,checkpoint_no,generation,state_payload) VALUES(%s,%s,'',%s,1,1,%s)",(t,rid,cp,Jsonb(dict(private='PRIVATE-CP'))))
  await self.execute("INSERT INTO {s}.checkpoint_writes(tenant_id,run_id,namespace,checkpoint_id,task_id,write_index,task_path,channel,value,generation) VALUES(%s,%s,'',%s,'fixture',0,'path','channel','null'::jsonb,1)",(t,rid,cp))
  await self.execute("INSERT INTO {s}.rag_attempts(tenant_id,run_id,kind,job_id,generation,epoch,request_digest,reserved_input_tokens,reserved_output_tokens,settlement_hash,settlement_until) SELECT tenant_id,id,'chat',job_id,1,0,%s,4096,256,%s,clock_timestamp()+interval '1 hour' FROM {s}.runs WHERE tenant_id=%s AND id=%s",('a'*64,hashlib.sha256(('a'*64).encode()).hexdigest(),t,rid))
  current=await inventory(cid);data=dict(key=str(uuid4()),version=current['version'],content_digest=current['content_digest'],policy_hash=current['effect_policy_hash'])
  # Actual writer overlap: change private content while fence lock admission
  # waits. The old snapshot must fail after the lock is released.
  task=None
  try:
   async with self.admin.transaction():
    await self.execute('SELECT id FROM {s}.conversations WHERE tenant_id=%s AND id=%s FOR UPDATE',(t,UUID(cid)))
    prepared=await headers()
    task=asyncio.create_task(browser.post('/api/v1/privacy/conversations/'+cid+'/deletion',json=data,headers=prepared));waiting=False
    for _ in range(40):
     await self.execute('SELECT pg_stat_clear_snapshot()')
     waiting=await self.scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=%s AND wait_event_type='Lock' AND query LIKE %s)",('eai_runtime_'+self.schema.rsplit('_',1)[1],'%privacy_conversation_fence_call%'))
     if waiting:break
     await asyncio.sleep(.025)
    require(waiting,'fence-root-wait-observed')
    await self.execute("UPDATE {s}.checkpoints SET state_payload=%s WHERE tenant_id=%s AND run_id=%s",(Jsonb(dict(private='PRIVATE-CHANGED-WHILE-WAIT')),t,rid))
   raced=await task;require(raced.status_code==409,'fence-post-wait-content-CAS')
  finally:
   if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
  require((await status(cid)).json()['fenced'] is False,'fence-race-no-partial-fence')
  current=await inventory(cid);data.update(content_digest=current['content_digest'],version=current['version'])
  self.passed('actual_root_lock_overlap_rechecks_snapshot_after_wait_and_rolls_back_stale_fence')
  reply=await post(cid,data);require(reply.status_code==202 and reply.json()['fenced'],'fence-remaining-fixture-admission')
  # Exercise owning boundaries directly as fixture owner. A restricted runtime
  # has no raw table grants at all; these prove triggers also cover old producers.
  statements=[
   ("UPDATE {s}.conversations SET manual_name='late' WHERE tenant_id=%s AND id=%s",(t,UUID(cid))),
   ("UPDATE {s}.messages SET body='late' WHERE tenant_id=%s AND run_id=%s",(t,rid)),
   ("UPDATE {s}.checkpoints SET state_payload='{{}}'::jsonb WHERE tenant_id=%s AND run_id=%s",(t,rid)),
   ("UPDATE {s}.checkpoint_writes SET value='null'::jsonb WHERE tenant_id=%s AND run_id=%s",(t,rid)),
   ("UPDATE {s}.runs SET writer_generation=writer_generation+1 WHERE tenant_id=%s AND id=%s",(t,rid)),
  ]
  for query,args in statements:
   blocked=False
   try:await self.admin.execute(sql.SQL(query).format(s=sql.Identifier(self.schema)),args)
   except CheckViolation:blocked=True
   require(blocked,'fence-owning-boundary')
  late=await browser.post('/api/v1/conversations/'+cid+'/messages',json=dict(text='late',client_message_id=str(uuid4())),headers=await headers());require(late.status_code>=400,'fence-late-public-append')
  fresh=await browser.post('/api/v1/conversations/'+control+'/messages',json=dict(text='control',client_message_id=str(uuid4())),headers=await headers());require(fresh.status_code==202,'control-continues')
  after=await inventory(cid);require(after['content_digest']==current['content_digest'] and after['unknown_provider_attempts']==1 and after['erased'] is False,'fence-preserves-content-and-unknown-hold')
  self.passed('actual_owning_content_checkpoint_pendingwrite_run_and_public_append_cannot_grow_fenced_root_while_other_root_works')
  # Settle an existing provider attempt after the run is fenced; no SDK request.
  async with self.database.transaction() as connection:
   value=(await (await connection.execute('SELECT '+self.schema+'.rag_usage_call(%s)',(Jsonb(dict(tenant_id=str(t),run_id=str(rid),kind='chat',receipt='a'*64,input_tokens=2,output_tokens=3)),))).fetchone())[0]
  require(value['status']=='ok','fence-usage-settlement-'+str(value.get('status')))
  require((await inventory(cid))['unknown_provider_attempts']==0,'fence-known-usage-retained')
  self.passed('existing_usage_receipt_can_settle_after_fence_without_new_provider_request_or_content_write')


def main():
    logging.disable(logging.CRITICAL);p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');a=p.parse_args()
    if not a.live:p.error('Explicit --live required for owned disposable fixture.')
    folder=ROOT/'local/data01/fence-remainder';folder.mkdir(parents=True,exist_ok=True);result=dict(passed=False)
    files=list((ROOT/'backend/src').rglob('*.py'))+list((ROOT/'backend/src').rglob('*.sql'))+[Path(__file__),ROOT/'scripts/checks/check_privacy_inventory.py',ROOT/'scripts/checks/check_privacy_effect_registry.py',ROOT/'scripts/checks/check_privacy_registry.py'];hashes={f.relative_to(ROOT).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
    try:
        settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=35,operation='privacy_inventory',direct_timeout=240))
        require(all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h for n,h in hashes.items()),'expiry-source-stable')
        result.pop('ingestion',None);result.update(source_sha256=hashes,scope='Actual owning conversation fence and exact-version/policy binding only. Unknown backup/provider proofs still held; no erase/export/SDK/AWS. Synthetic approval durations, installed schema unchanged.')
    except Exception as error:
        result.update(error_type=type(error).__name__,check=str(error) if isinstance(error,CheckFailure) else 'sanitized_failure',frames=[dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(error.__traceback__)])
    (folder/'database-proof.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}));raise SystemExit(0 if result['passed'] else 1)


if __name__=='__main__':main()
