"""Actual response-only conversation export and current-source/privacy gates."""
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
  require((await browser.post('/api/v1/operator/privacy-policy',json=legacy,headers=await headers(op))).status_code==201,'export-v1-fixture')
  policy=effect_payload();policy['copy_rules'] += [dict(category=c,store=s,copy_class=cl,min_retain_seconds=0,expire_after_seconds=ttl,clock_basis=clock,regions=[region],provider_retention_seconds='not_applicable',training_allowed=False) for c,s,cl,ttl,clock,region in [('conversations','cache','export_artifact',60,'copy_created_at','local-test'),('audit','db','audit_metadata',3600,'created_at','ap-southeast-1')]]
  require((await browser.post('/api/v1/operator/privacy-effect-policy',json=policy,headers=await headers(op))).status_code==201,'export-v2-fixture')
  appended=await browser.post('/api/v1/conversations/'+cid+'/messages',json=dict(text='PRIVATE-OWN-HUMAN',client_message_id=str(uuid4())),headers=await headers());require(appended.status_code==202,'export-human');rid=UUID(appended.json()['run_id'])
  known=uuid4();hidden=uuid4()
  await self.execute("INSERT INTO {s}.messages(tenant_id,id,conversation_id,author_kind,body,sequence,run_id,output_kind,provenance_mode) VALUES(%s,%s,%s,'model','PRIVATE-SOURCE-FREE',2,%s,'answer','source_free'),(%s,%s,%s,'model','PRIVATE-UNVERIFIED',3,%s,'answer','unverified')",(t,known,UUID(cid),rid,t,hidden,UUID(cid),rid))
  await self.execute("INSERT INTO {s}.feedback(tenant_id,message_id,conversation_id,actor_id,rating,reason) VALUES(%s,%s,%s,%s,'up','PRIVATE-OWN-FEEDBACK'),(%s,%s,%s,%s,'down','PRIVATE-HIDDEN-FEEDBACK')",(t,known,UUID(cid),person,t,hidden,UUID(cid),person))
  cp=uuid4();await self.execute("INSERT INTO {s}.checkpoints(tenant_id,run_id,namespace,id,checkpoint_no,generation,state_payload) VALUES(%s,%s,'',%s,1,1,%s)",(t,rid,cp,Jsonb(dict(private='PRIVATE-NEVER-EXPORT-RUNTIME-STATE'))))
  before=await inventory(cid);data=dict(key=str(uuid4()),version=before['version'],content_digest=before['content_digest'],policy_hash=before['effect_policy_hash'])
  async def export(body=data,index=0,target=cid):return await browser.post('/api/v1/privacy/conversations/'+target+'/export',json=body,headers=await headers(index))
  await self.execute("INSERT INTO {s}.privacy_store_locations(runtime_role,store,region,verified_at,expires_at,evidence_sha256,environment) SELECT role_oid,'cache','local-test',clock_timestamp(),clock_timestamp()+interval '1 hour',%s,'fictional_fixture' FROM {s}.runtime_role_registry UNION ALL SELECT role_oid,'db','ap-southeast-1',clock_timestamp(),clock_timestamp()+interval '1 hour',%s,'fictional_fixture' FROM {s}.runtime_role_registry",('a'*64,'b'*64))
  # Change source classification without changing private byte count or root;
  # a cached export key must never replay a once-visible raw model answer.
  await self.execute("UPDATE {s}.messages SET provenance_mode='unverified' WHERE tenant_id=%s AND id=%s",(t,known))
  changed=await inventory(cid);fresh=dict(data,key=str(uuid4()),version=changed['version'],content_digest=changed['content_digest'])
  current=await export(fresh);require(current.status_code==200,'export-expiry-fixture')

  # Seed an expired immutable receipt, as if a prior response had been lost.
  expired=dict(fresh,key=str(uuid4()));request_hash=await self.scalar("SELECT encode(sha256(convert_to(%s::jsonb::text,'UTF8')),'hex')",(Jsonb(dict(expired,id=cid)),))
  await self.execute("INSERT INTO {s}.privacy_export_receipts(tenant_id,actor_id,key,conversation_id,session_id,membership_revision,request_hash,policy_revision,content_digest,created_at,expires_at,metadata_expires_at) SELECT tenant_id,actor_id,%s,conversation_id,session_id,membership_revision,%s,policy_revision,content_digest,clock_timestamp()-interval '20 seconds',clock_timestamp()-interval '10 seconds',metadata_expires_at FROM {s}.privacy_export_receipts WHERE tenant_id=%s AND key=%s",(UUID(expired['key']),request_hash,t,UUID(fresh['key'])))
  require((await export(expired)).status_code==410,'export-expired-not-regenerated')
  require(await self.scalar('SELECT count(*) FROM {s}.privacy_export_receipts')==2,'export-no-extra-expired-artifact')
  self.passed('actual_expired_creation_bound_response_is_gone_without_regeneration_or_TTL_refresh')


def main():
    logging.disable(logging.CRITICAL);p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');a=p.parse_args()
    if not a.live:p.error('Explicit --live required for owned disposable fixture.')
    folder=ROOT/'local/data01/export-delta';folder.mkdir(parents=True,exist_ok=True);result=dict(passed=False)
    files=list((ROOT/'backend/src').rglob('*.py'))+list((ROOT/'backend/src').rglob('*.sql'))+[Path(__file__),ROOT/'scripts/checks/check_privacy_inventory.py',ROOT/'scripts/checks/check_privacy_effect_registry.py',ROOT/'scripts/checks/check_privacy_registry.py'];hashes={f.relative_to(ROOT).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
    try:
        settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=37,operation='privacy_inventory',direct_timeout=240))
        require(all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h for n,h in hashes.items()),'expiry-source-stable')
        result.pop('ingestion',None);result.update(source_sha256=hashes,scope='Actual private response-only authorized content export. No stored raw archive; finite approved memory-copy expiry, metadata-only DB receipt. Physical location facts are fictional fixture enrollment; production location/provider/backup proof not claimed.')
    except Exception as error:
        result.update(error_type=type(error).__name__,check=str(error) if isinstance(error,CheckFailure) else 'sanitized_failure',frames=[dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(error.__traceback__)])
    (folder/'database-proof.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}));raise SystemExit(0 if result['passed'] else 1)


if __name__=='__main__':main()
