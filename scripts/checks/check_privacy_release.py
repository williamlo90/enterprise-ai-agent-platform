"""Actual durable cancellation and permanently stale prior run fixture."""
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
  from uuid import UUID
  cid=await create();other=await create(4);t=self.people[0]['tenant'];person=self.people[0]['person']
  legacy=dict(key=str(uuid4()),revision=1,expires_at=int(time.time())+3600,rules=[dict(category=c.value,retention_seconds=10,regions=['ap-southeast-1'],stores=sorted(REQUIRED[c]),provider_retention_seconds=0,training_allowed=False) for c in Category])
  require((await browser.post('/api/v1/operator/privacy-policy',json=legacy,headers=await headers(op))).status_code==201,'release-v1-fixture')
  require((await browser.post('/api/v1/operator/privacy-effect-policy',json=effect_payload(),headers=await headers(op))).status_code==201,'release-v2-fixture')
  old=await browser.post('/api/v1/conversations/'+cid+'/messages',json=dict(text='fictional old input',client_message_id=str(uuid4())),headers=await headers());require(old.status_code==202,'release-old-run');rid=UUID(old.json()['run_id'])
  executor=await self.caller('executor',t)
  await self.execute("UPDATE {s}.outbox_jobs SET state='running',generation=1,attempts=1,worker_id=(SELECT id FROM {s}.job_callers WHERE credential_hash=%s),lease_until=clock_timestamp()+interval '5 minutes',attempt_deadline=clock_timestamp()+interval '5 minutes' WHERE tenant_id=%s AND target_id=%s",(executor._credential_hash,t,rid))
  jid=await self.scalar('SELECT job_id FROM {s}.runs WHERE tenant_id=%s AND id=%s',(t,rid))
  old_binding=dict(credential_hash=executor._credential_hash,tenant_id=str(t),run_id=str(rid),job_id=str(jid),generation=1,epoch=0,sources=[],namespace='',checkpoint_id=None)
  async def stage():
   async with self.database.transaction() as connection:
    return (await (await connection.execute('SELECT '+self.schema+".checkpoint_call('stage',%s)",(Jsonb(old_binding),))).fetchone())[0]
  require((await stage())['status']=='ok','release-old-job-actually-admitted-before-fence')
  before=await inventory(cid);request=dict(key=str(uuid4()),version=before['version'],content_digest=before['content_digest'],policy_hash=before['effect_policy_hash'])
  require((await post(cid,request)).status_code==202,'release-fence-fixture')
  async def release(data,index=0,target=cid):return await browser.post('/api/v1/privacy/conversations/'+target+'/deletion/cancel',json=data,headers=await headers(index))
  data=dict(key=str(uuid4()),fence_key=request['key'])
  for index,code in [(4,404),(op,403)]:require((await release(data,index)).status_code==code,'release-exact-owner')
  require((await release(data,target=other)).status_code==404,'release-other-root')
  require((await release(dict(data,fence_key=str(uuid4())))).status_code==404,'release-unknown-fence')
  result=await release(data);require(result.status_code==202 and result.json()['released'] and not result.json()['erased'],'release-accepted')
  require(result.headers.get('cache-control')=='no-store' and 'x-eai-context-binding' in result.headers,'release-G13')
  repeat=await release(data);require(repeat.status_code==202 and repeat.json()['replayed'],'release-stable-replay')
  require((await release(dict(data,key=str(uuid4())))).status_code==409,'release-does-not-rekey')
  require((await status(cid)).json()['fenced'] is False,'release-unfenced')
  require(await self.scalar('SELECT count(*) FROM {s}.privacy_fence_releases')==1,'release-one-record')
  self.passed('actual_current_owner_CSRF_G13_scoped_release_is_durable_exact_key_replay_and_cannot_release_other_fence')
  require((await stage())['status']=='stale','release-does-not-revive-old-valid-job')
  require(await self.scalar("SELECT state='failed' AND writer_generation=1 FROM {s}.runs WHERE tenant_id=%s AND id=%s",(t,rid)),'release-keeps-epoch')
  fresh=await browser.post('/api/v1/conversations/'+cid+'/messages',json=dict(text='fictional fresh input',client_message_id=str(uuid4())),headers=await headers());require(fresh.status_code==202 and fresh.json()['run_id']!=str(rid),'release-fresh-admission-only')
  self.passed('actual_previously_authorized_job_stays_stale_after_release_while_fresh_run_is_admitted')
  next_snapshot=await inventory(cid);next_request=dict(key=str(uuid4()),version=next_snapshot['version'],content_digest=next_snapshot['content_digest'],policy_hash=next_snapshot['effect_policy_hash'])
  require((await post(cid,next_request)).status_code==202,'release-new-cycle')
  replay_old=await post(cid,request);require(replay_old.status_code==202 and replay_old.json()['released'] and replay_old.json()['fenced'],'old-cycle-replay-must-report-current-fence')
  require((await status(cid)).json()['fenced'],'old-replay-not-current-release')
  require(await self.scalar('SELECT count(*) FROM {s}.privacy_conversation_fences WHERE tenant_id=%s AND conversation_id=%s',(t,UUID(cid)))==2,'two-immutable-fence-cycles')
  require(await self.scalar('SELECT original_session_id=(SELECT id FROM {s}.sessions WHERE token_hash=%s) AND original_membership_id IS NOT NULL AND original_access_revision IS NOT NULL FROM {s}.privacy_conversation_fences WHERE tenant_id=%s AND key=%s',(digest('session',self.people[0]['token']),t,UUID(next_request['key']))),'original-authority-server-bound')
  await self.execute('INSERT INTO {s}.privacy_conversation_effect_intents(tenant_id,actor_id,fence_key,effect_key) VALUES(%s,%s,%s,%s)',(t,person,UUID(next_request['key']),uuid4()))
  require((await release(dict(key=str(uuid4()),fence_key=next_request['key']))).status_code==409,'release-cannot-ignore-pending-effect')
  require((await status(cid)).json()['fenced'],'effect-intent-keeps-hold')
  self.passed('actual_new_cycle_never_rearms_old_request_and_pending_effect_intent_blocks_release')


def main():
    logging.disable(logging.CRITICAL);p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');a=p.parse_args()
    if not a.live:p.error('Explicit --live required for owned disposable fixture.')
    folder=ROOT/'local/data01/release-actual';folder.mkdir(parents=True,exist_ok=True);result=dict(passed=False)
    files=list((ROOT/'backend/src').rglob('*.py'))+list((ROOT/'backend/src').rglob('*.sql'))+[Path(__file__),ROOT/'scripts/checks/check_privacy_inventory.py',ROOT/'scripts/checks/check_privacy_effect_registry.py',ROOT/'scripts/checks/check_privacy_registry.py'];hashes={f.relative_to(ROOT).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
    try:
        settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=36,operation='privacy_inventory',direct_timeout=240))
        require(all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h for n,h in hashes.items()),'expiry-source-stable')
        result.pop('ingestion',None);result.update(source_sha256=hashes,scope='Actual durable current-owner release, fresh cycle and actual old checkpoint job rejection. Existing fixture job explicitly leased before fence; no scheduler concurrency or model/storage erasure/export claim.')
    except Exception as error:
        result.update(error_type=type(error).__name__,check=str(error) if isinstance(error,CheckFailure) else 'sanitized_failure',frames=[dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(error.__traceback__)])
    (folder/'database-proof.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}));raise SystemExit(0 if result['passed'] else 1)


if __name__=='__main__':main()
