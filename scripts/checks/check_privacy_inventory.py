"""Owned actual conversation inventory. No fence, erase, export or model claim."""
import argparse,asyncio,copy,hashlib,json,logging,time,traceback
from pathlib import Path
from uuid import UUID,uuid4
from unittest.mock import patch
from psycopg.types.json import Jsonb
import check_privacy_registry as registry_probe
from check_privacy_registry import Harness as RegistryHarness
from check_knowledge_workspace import verify
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.modules.privacy.domain import Category,REQUIRED
from enterprise_ai_platform.file_check import ORIGIN

ROOT=Path(__file__).resolve().parents[2]


class Harness(RegistryHarness):
    async def privacy_inventory(self):
        def enabled(**values):return Settings(**dict(values,conversations_enabled=True))
        with patch.object(registry_probe,'Settings',enabled):await super().privacy_registry()

    async def cases(self,browser,resources,op,foreign,wrong_scope):
        async def headers(index=0):
            p=self.people[index];cookies={'cookie':'__Host-eai-session='+p['token']}
            me=await browser.get('/api/v1/me',headers=dict(cookies,**{'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'}));require(me.status_code==200,'inventory-bootstrap');p['csrf']=me.json()['csrf_token']
            return dict(cookies,origin=ORIGIN,**{'content-type':'application/json','x-csrf-token':p['csrf'],'x-eai-expected-context':me.json()['context_binding']})
        async def create(index=0):
            response=await browser.post('/api/v1/conversations',json=dict(language='en',key=str(uuid4())),headers=await headers(index));require(response.status_code==201,'inventory-create');return response.json()['id']
        async def inventory(cid,index=0):return await browser.get('/api/v1/privacy/conversations/'+cid+'/inventory',headers=await headers(index))
        cid=await create();control=await create();other=await create(4)
        require((await inventory(cid)).status_code==503,'missing-policy-held')
        policy=dict(key=str(uuid4()),revision=1,expires_at=int(time.time())+3600,rules=[dict(category=c.value,retention_seconds=10,regions=['ap-southeast-1'],stores=sorted(REQUIRED[c]),provider_retention_seconds=0,training_allowed=False) for c in Category])
        p=await browser.post('/api/v1/operator/privacy-policy',json=policy,headers=await headers(op));require(p.status_code==201,'inventory-policy-fixture')
        for target,index in [(cid,4),(other,0),(cid,2)]:require((await inventory(target,index)).status_code==404,'inventory-owner-scope')
        require((await inventory(cid,op)).status_code==403,'config-not-content')
        self.passed('actual_inventory_requires_effective_policy_and_exact_conversation_owner_not_config_admin')
        appended=await browser.post('/api/v1/conversations/'+cid+'/messages',json=dict(text='PRIVATE-HUMAN-MATERIAL',client_message_id=str(uuid4())),headers=await headers());require(appended.status_code==202,'inventory-append');rid=UUID(appended.json()['run_id'])
        tenant=self.people[0]['tenant'];person=self.people[0]['person'];mid=uuid4();cpid=uuid4()
        # Seed private copies at their actual storage boundaries, not fake store labels.
        await self.execute("INSERT INTO {s}.messages(tenant_id,id,conversation_id,author_kind,body,sequence,run_id,output_kind,provenance_mode) VALUES(%s,%s,%s,'model','PRIVATE-MODEL-MATERIAL',2,%s,'answer','unverified')",(tenant,mid,UUID(cid),rid))
        await self.execute("INSERT INTO {s}.feedback(tenant_id,message_id,conversation_id,actor_id,rating,reason) VALUES(%s,%s,%s,%s,'up','PRIVATE-FEEDBACK-MATERIAL')",(tenant,mid,UUID(cid),person))
        await self.execute("INSERT INTO {s}.checkpoints(tenant_id,run_id,namespace,id,checkpoint_no,generation,state_payload) VALUES(%s,%s,'',%s,1,1,%s)",(tenant,rid,cpid,Jsonb(dict(private='PRIVATE-CHECKPOINT-MATERIAL'))))
        await self.execute("INSERT INTO {s}.checkpoint_writes(tenant_id,run_id,namespace,checkpoint_id,task_id,write_index,task_path,channel,value,generation) VALUES(%s,%s,'',%s,'fixture',0,'private-fixture-path','private-channel',%s,1)",(tenant,rid,cpid,Jsonb(dict(private='PRIVATE-PENDING-WRITE'))))
        await self.execute('UPDATE {s}.conversations SET manual_name=%s WHERE tenant_id=%s AND id=%s',('PRIVATE-NAME',tenant,UUID(cid)))
        current=await inventory(cid);require(current.status_code==200,'inventory-positive-'+str(current.status_code));body=current.json();counts=body['counts']
        require(counts['messages']==2 and counts['feedback']==1 and counts['checkpoints']==1 and counts['pending_writes']==1,'inventory-all-private-copies')
        require(all(marker not in current.text for marker in ['PRIVATE-HUMAN','PRIVATE-MODEL','PRIVATE-FEEDBACK','PRIVATE-CHECKPOINT','PRIVATE-PENDING','PRIVATE-NAME']),'inventory-metadata-only')
        require(body['execution_state']=='held' and body['erased'] is False and body['residuals']['backup'].startswith('unverified') and body['residuals']['provider'].startswith('unverified'),'inventory-no-false-erasure')
        require('x-eai-context-binding' in current.headers and current.headers.get('cache-control')=='no-store','inventory-guard-headers')
        self.passed('actual_message_feedback_checkpoint_pendingwrite_and_name_inventory_exposes_only_metadata_and_unknown_store_holds')
        before=body['content_digest']
        await self.execute('UPDATE {s}.feedback SET reason=%s,version=version+1 WHERE tenant_id=%s AND message_id=%s',('PRIVATE-FEEDBACK-CHANGED',tenant,mid))
        changed=await inventory(cid);require(changed.status_code==200 and changed.json()['counts']==counts and changed.json()['content_digest']!=before,'inventory-digest-not-only-count')
        untouched=await inventory(control);require(untouched.status_code==200 and untouched.json()['counts']['messages']==0,'control-conversation-untouched')
        self.passed('same_count_content_drift_changes_inventory_digest_and_control_conversation_is_untouched')
        await self.execute("INSERT INTO {s}.checkpoint_writes(tenant_id,run_id,namespace,checkpoint_id,task_id,write_index,task_path,channel,value,generation) SELECT %s,%s,'',%s,'bulk-'||n,0,'fixture','fixture','null'::jsonb,1 FROM generate_series(1,520) n",(tenant,rid,cpid))
        require((await inventory(cid)).status_code==503,'inventory-bound-no-truncation')
        require((await inventory(control)).status_code==200,'inventory-bound-does-not-disable-other-root')
        self.passed('oversized_actual_inventory_is_held_instead_of_aggregating_unbounded_or_truncating_scope')


def main():
    logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned disposable fixture.')
    folder=ROOT/'local/data01/inventory-actual';folder.mkdir(parents=True,exist_ok=True);report=dict(passed=False)
    files=list((ROOT/'backend/src').rglob('*.py'))+list((ROOT/'backend/src').rglob('*.sql'))+[Path(__file__)]
    hashes={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    try:
        settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:report=runner.run(verify(direct,base,harness_type=Harness,target_version=32,operation='privacy_inventory',direct_timeout=240))
        require(all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h for n,h in hashes.items()),'inventory-source-stable')
        report.pop('ingestion',None);report.update(source_sha256=hashes,scope='Actual configured32 original-session policy-bound conversation inventory metadata. Public API creates root/human run; private copy rows seeded in actual tables. No fence/erase/export/provider/backups acceptance.')
    except Exception as error:
        report.update(error_type=type(error).__name__,check=str(error) if isinstance(error,CheckFailure) else 'sanitized_failure',frames=[dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(error.__traceback__)])
        chain=[];current=error
        for _ in range(8):
            if current is None:break
            item=dict(type=type(current).__name__,sqlstate=getattr(current,'sqlstate',None))
            if getattr(current,'sqlstate',None)=='42601':item['syntax']=getattr(getattr(current,'diag',None),'message_primary','syntax rejected')
            chain.append(item);current=current.__context__
        report['diagnostics']=chain
    (folder/'database-proof.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8');print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}));raise SystemExit(0 if report['passed'] else 1)


if __name__=='__main__':main()
