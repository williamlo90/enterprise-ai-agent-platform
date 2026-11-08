"""Focused actual namespace-association/deterministic-order delta only."""
import hashlib,json,time
from pathlib import Path
from uuid import UUID,uuid4
from psycopg.types.json import Jsonb
import check_privacy_inventory as probe
from enterprise_ai_platform.authorization_check import require
from enterprise_ai_platform.modules.privacy.domain import Category,REQUIRED
from enterprise_ai_platform.file_check import ORIGIN

ROOT=Path(__file__).resolve().parents[2]


class NamespaceHarness(probe.Harness):
    async def cases(self,browser,resources,op,foreign,wrong_scope):
        async def headers(index=0):
            p=self.people[index];cookies={'cookie':'__Host-eai-session='+p['token']}
            me=await browser.get('/api/v1/me',headers=dict(cookies,**{'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'}));require(me.status_code==200,'namespace-bootstrap');p['csrf']=me.json()['csrf_token']
            return dict(cookies,origin=ORIGIN,**{'content-type':'application/json','x-csrf-token':p['csrf'],'x-eai-expected-context':me.json()['context_binding']})
        policy=dict(key=str(uuid4()),revision=1,expires_at=int(time.time())+3600,rules=[dict(category=c.value,retention_seconds=10,regions=['ap-southeast-1'],stores=sorted(REQUIRED[c]),provider_retention_seconds=0,training_allowed=False) for c in Category])
        require((await browser.post('/api/v1/operator/privacy-policy',json=policy,headers=await headers(op))).status_code==201,'namespace-policy-fixture')
        created=await browser.post('/api/v1/conversations',json=dict(language='en',key=str(uuid4())),headers=await headers());require(created.status_code==201,'namespace-root-fixture');cid=created.json()['id']
        admitted=await browser.post('/api/v1/conversations/'+cid+'/messages',json=dict(text='PRIVATE-NAMESPACE-INPUT',client_message_id=str(uuid4())),headers=await headers());require(admitted.status_code==202,'namespace-run-fixture');rid=UUID(admitted.json()['run_id']);cpid=uuid4();tenant=self.people[0]['tenant']
        for ns in ['alpha','beta']:
            await self.execute('INSERT INTO {s}.checkpoints(tenant_id,run_id,namespace,id,checkpoint_no,generation,state_payload) VALUES(%s,%s,%s,%s,1,1,%s)',(tenant,rid,ns,cpid,Jsonb(dict(private='PRIVATE-NAMESPACE-STATE'))))
        async def put(ns):
            await self.execute("INSERT INTO {s}.checkpoint_writes(tenant_id,run_id,namespace,checkpoint_id,task_id,write_index,task_path,channel,value,generation) VALUES(%s,%s,%s,%s,'same-task',0,'same-path','same-channel',%s,1)",(tenant,rid,ns,cpid,Jsonb(dict(private='PRIVATE-NAMESPACE-WRITE'))))
        async def inventory():
            result=await browser.get('/api/v1/privacy/conversations/'+cid+'/inventory',headers=await headers());require(result.status_code==200,'namespace-inventory-'+str(result.status_code));require('PRIVATE-' not in result.text,'namespace-no-raw-content');return result.json()
        await put('alpha');first=await inventory();require((await inventory())['content_digest']==first['content_digest'],'namespace-stable-before')
        await self.execute("UPDATE {s}.checkpoint_writes SET namespace='beta' WHERE tenant_id=%s AND run_id=%s",(tenant,rid))
        moved=await inventory();require(moved['counts']==first['counts'] and moved['content_digest']!=first['content_digest'],'namespace-association-detected')
        require((await inventory())['content_digest']==moved['content_digest'],'namespace-stable-after')
        self.passed('actual_samecount_namespace_only_pendingwrite_reassociation_changes_digest_without_raw_content')
        await put('alpha');ordered=await inventory()
        await self.execute('DELETE FROM {s}.checkpoint_writes WHERE tenant_id=%s AND run_id=%s',(tenant,rid))
        await put('beta');await put('alpha');reversed_rows=await inventory()
        require(reversed_rows['counts']==ordered['counts'] and reversed_rows['content_digest']==ordered['content_digest'],'namespace-order-canonical')
        self.passed('physical_reinsertion_order_does_not_change_namespace_canonical_inventory_digest')


if __name__=='__main__':
    own=hashlib.sha256(Path(__file__).read_bytes()).hexdigest();probe.Harness=NamespaceHarness
    # Base entry owns normal disposable setup/cleanup and current application hashes.
    # Preserve both the earlier proof and this wrapper's exact fingerprint.
    try:probe.main()
    except SystemExit as done:
        if done.code!=0:raise
        assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest()==own
        p=ROOT/'local/data01/inventory-actual/database-proof.json';result=json.loads(p.read_text(encoding='utf-8'));result['source_sha256'][Path(__file__).relative_to(ROOT).as_posix()]=own
        result['scope']='Focused namespace association and canonical ordering regression with actual configured32 inventory; no repeat of prior owner/policy/oversize checks; no erasure/export.'
        out=ROOT/'local/data01/inventory-namespace-proof.json';out.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
