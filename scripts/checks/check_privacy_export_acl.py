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
 async def ready_file(self,text='Fictional policy.',index=0):
  # Current JobService returns a normalized lease, without a status key.
  response=await self.upload(text.encode(),index=index);require(response.status_code==202,'acl-upload-fixture')
  claim=await self.scheduler.claim();require(claim.get('job_type')=='file.inspect','acl-file-lease')
  await self.inspector.process(claim);return response.json()['id']

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
  row=await self.document('PRIVATE-ACL-DOCUMENT')
  require((await self.mutate(row,'publish')).status_code==200,'export-ACL-published-fixture')
  cid=await create(4);t=self.people[4]['tenant'];vid=UUID(row['version_id']);version=await self.scalar('SELECT version_no FROM {s}.document_versions WHERE tenant_id=%s AND id=%s',(t,vid));revision=await self.scalar('SELECT access_revision FROM {s}.documents WHERE tenant_id=%s AND id=%s',(t,UUID(row['document_id'])))
  legacy=dict(key=str(uuid4()),revision=1,expires_at=int(time.time())+3600,rules=[dict(category=c.value,retention_seconds=10,regions=['ap-southeast-1'],stores=sorted(REQUIRED[c]),provider_retention_seconds=0,training_allowed=False) for c in Category])
  require((await browser.post('/api/v1/operator/privacy-policy',json=legacy,headers=await headers(op))).status_code==201,'acl-v1-fixture')
  policy=effect_payload();policy['copy_rules'] += [dict(category=c,store=s,copy_class=cl,min_retain_seconds=0,expire_after_seconds=ttl,clock_basis=clock,regions=[region],provider_retention_seconds='not_applicable',training_allowed=False) for c,s,cl,ttl,clock,region in [('conversations','cache','export_artifact',60,'copy_created_at','local-test'),('audit','db','audit_metadata',3600,'created_at','ap-southeast-1')]]
  require((await browser.post('/api/v1/operator/privacy-effect-policy',json=policy,headers=await headers(op))).status_code==201,'acl-v2-fixture')
  await self.execute("INSERT INTO {s}.privacy_store_locations(runtime_role,store,region,verified_at,expires_at,evidence_sha256,environment) SELECT role_oid,'cache','local-test',clock_timestamp(),clock_timestamp()+interval '1 hour',%s,'fictional_fixture' FROM {s}.runtime_role_registry UNION ALL SELECT role_oid,'db','ap-southeast-1',clock_timestamp(),clock_timestamp()+interval '1 hour',%s,'fictional_fixture' FROM {s}.runtime_role_registry",('a'*64,'b'*64))
  if getattr(self,'skip_acl_checks',False):
   self.privacy_fixture_document=row;return
  appended=await browser.post('/api/v1/conversations/'+cid+'/messages',json=dict(text='fictional question',client_message_id=str(uuid4())),headers=await headers(4));require(appended.status_code==202,'acl-human');rid=UUID(appended.json()['run_id']);mid=uuid4()
  await self.execute("INSERT INTO {s}.messages(tenant_id,id,conversation_id,author_kind,body,sequence,run_id,output_kind,provenance_mode) VALUES(%s,%s,%s,'model','PRIVATE-ACL-MODEL',2,%s,'answer','dependent')",(t,mid,UUID(cid),rid))
  await self.execute("INSERT INTO {s}.source_dependencies(tenant_id,id,conversation_id,output_message_id,source_system,resource_type,resource_id,resource_version,required_relation,observed_revision,platform_document_version_id) VALUES(%s,%s,%s,%s,'platform','document_version',%s,%s,'read',%s,%s)",(t,uuid4(),UUID(cid),mid,vid,str(version),str(revision),vid))
  response=await browser.get('/api/v1/privacy/conversations/'+cid+'/inventory',headers=await headers(4));require(response.status_code==200,'acl-inventory');snapshot=response.json();data=dict(key=str(uuid4()),version=snapshot['version'],content_digest=snapshot['content_digest'],policy_hash=snapshot['effect_policy_hash'])
  async def export(body):return await browser.post('/api/v1/privacy/conversations/'+cid+'/export',json=body,headers=await headers(4))
  first=await export(data);require(first.status_code==200 and 'PRIVATE-ACL-MODEL' in first.text,'acl-allowed-source-export')
  require((await self.mutate(row,'acl',readers=[])).status_code==200,'acl-revoke-real-producer')
  response=await browser.get('/api/v1/privacy/conversations/'+cid+'/inventory',headers=await headers(4));require(response.status_code==200 and response.json()['content_digest']==snapshot['content_digest'] and response.json()['version']==snapshot['version'],'acl-revoke-without-content-change')
  require((await export(data)).status_code==409,'acl-no-old-export-replay')
  fresh=await export(dict(data,key=str(uuid4())));require(fresh.status_code==200 and 'PRIVATE-ACL-MODEL' not in fresh.text and any(m.get('redacted') for m in fresh.json()['content']['messages']),'acl-new-export-redacts-model')
  require(await self.scalar("SELECT body='PRIVATE-ACL-MODEL' FROM {s}.messages WHERE tenant_id=%s AND id=%s",(t,mid)),'acl-not-erasure-or-content-drift')
  self.privacy_fixture_document=row
  self.passed('actual_document_ACL_revoke_with_unchanged_conversation_snapshot_blocks_cached_export_and_redacts_new_download')


def main():
    logging.disable(logging.CRITICAL);p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');a=p.parse_args()
    if not a.live:p.error('Explicit --live required for owned disposable fixture.')
    folder=ROOT/'local/data01/export-acl-delta';folder.mkdir(parents=True,exist_ok=True);result=dict(passed=False)
    files=list((ROOT/'backend/src').rglob('*.py'))+list((ROOT/'backend/src').rglob('*.sql'))+[Path(__file__),ROOT/'scripts/checks/check_privacy_inventory.py',ROOT/'scripts/checks/check_privacy_effect_registry.py',ROOT/'scripts/checks/check_privacy_registry.py'];hashes={f.relative_to(ROOT).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
    try:
        settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=37,operation='privacy_inventory',direct_timeout=240))
        require(all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h for n,h in hashes.items()),'expiry-source-stable')
        result.pop('ingestion',None);result.update(source_sha256=hashes,scope='Actual unchanged-content document ACL revocation through owning knowledge producer. Controlled file/parser/embedding setup, actual restricted source gate/export. No external model/cloud/location evidence claim.')
    except Exception as error:
        result.update(inner_frames=[dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(error.__context__.__traceback__)] if error.__context__ else [],error_type=type(error).__name__,check=str(error) if isinstance(error,CheckFailure) else 'sanitized_failure',frames=[dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(error.__traceback__)])
    (folder/'database-proof.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}));raise SystemExit(0 if result['passed'] else 1)


if __name__=='__main__':main()
