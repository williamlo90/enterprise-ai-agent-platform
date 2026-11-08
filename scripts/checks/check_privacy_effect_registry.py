"""Actual scoped v2 policy approval; never storage erasure or artifact creation."""
import argparse,asyncio,copy,hashlib,json,logging,time,traceback
from pathlib import Path
from uuid import uuid4
from psycopg.types.json import Jsonb
from check_privacy_registry import Harness as RegistryHarness
from check_knowledge_workspace import verify
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.modules.identity.service import digest
from enterprise_ai_platform.modules.privacy.domain import Category,REQUIRED
from enterprise_ai_platform.modules.privacy.ports import effect_policy_from_json
from enterprise_ai_platform.modules.privacy.domain.effects import CopyClass,COPY_CLOCK
from enterprise_ai_platform.file_check import ORIGIN

ROOT=Path(__file__).resolve().parents[2]


def payload():
    classes={'db':CopyClass.CONTENT,'file':CopyClass.CONTENT,'index':CopyClass.INDEX,'cache':CopyClass.CACHE,'checkpoint':CopyClass.CHECKPOINT,'ledger':CopyClass.CANONICAL,'pins':CopyClass.CANONICAL,'backup':CopyClass.BACKUP,'provider':CopyClass.PROVIDER}
    rules=[dict(category=c.value,store=s,copy_class=classes[s].value,min_retain_seconds=0,expire_after_seconds=100,clock_basis='copy_created_at' if classes[s] in COPY_CLOCK else 'created_at',regions=['ap-southeast-1'],provider_retention_seconds=0 if s=='provider' else 'not_applicable',training_allowed=False) for c,stores in REQUIRED.items() for s in sorted(stores)]
    rules.append(dict(category='conversations',store='db',copy_class='export_artifact',min_retain_seconds='not_applicable',expire_after_seconds=60,clock_basis='copy_created_at',regions=['ap-southeast-1'],provider_retention_seconds='not_applicable',training_allowed=False))
    return dict(key=str(uuid4()),schema_version=2,revision=1,expires_at=int(time.time())+3600,copy_rules=rules)


class Harness(RegistryHarness):
    async def cases(self,browser,resources,op,foreign,wrong_scope):
        async def headers(index=op):
            p=self.people[index];cookies={'cookie':'__Host-eai-session='+p['token']}
            me=await browser.get('/api/v1/me',headers=dict(cookies,**{'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'}));require(me.status_code==200,'effect-bootstrap');p['csrf']=me.json()['csrf_token']
            return dict(cookies,origin=ORIGIN,**{'content-type':'application/json','x-csrf-token':p['csrf'],'x-eai-expected-context':me.json()['context_binding']})
        path='/api/v1/operator/privacy-effect-policy'
        async def get(index=op):return await browser.get(path,headers=await headers(index))
        async def post(body,index=op):return await browser.post(path,json=body,headers=await headers(index))
        legacy=dict(key=str(uuid4()),revision=1,expires_at=int(time.time())+3600,rules=[dict(category=c.value,retention_seconds=10,regions=['ap-southeast-1'],stores=sorted(REQUIRED[c]),provider_retention_seconds=0,training_allowed=False) for c in Category])
        require((await browser.post('/api/v1/operator/privacy-policy',json=legacy,headers=await headers())).status_code==201,'legacy-metadata-fixture')
        missing=await get();require(missing.status_code==200 and missing.json()['policy'] is None and missing.json()['effective'] is False,'no-v1-effect-fallback')
        for index in [0,3,wrong_scope]:require((await get(index)).status_code==403,'effect-role-denied')
        self.passed('v2_effect_registry_never_falls_back_to_v1_metadata_or_implicit_admin_permission')
        data=payload();first=await post(data);require(first.status_code==201,'effect-publish-'+str(first.status_code));record=first.json()['policy'];approved=effect_policy_from_json(record)
        require(record['schema_version']==2 and record['tenant_id']==str(self.people[op]['tenant']) and record['approved_by']==str(self.people[op]['person']) and len(approved.rules)==len(data['copy_rules']),'effect-exact-approval')
        replay=await post(data);require(replay.status_code==201 and replay.json()['replayed'] is True and replay.json()['receipt']==first.json()['receipt'],'effect-exact-replay')
        require(first.headers.get('cache-control')=='no-store' and 'x-eai-context-binding' in first.headers,'effect-G13')
        require(await self.scalar('SELECT count(*) FROM {s}.privacy_effect_policies')==1 and await self.scalar('SELECT count(*) FROM {s}.privacy_effect_decisions')==1,'effect-atomic-row-count')
        self.passed('actual_v2_copyclass_minmax_clock_and_artifact_bounds_share_atomic_original_actor_approval_receipt')
        invalid=[]
        x=copy.deepcopy(data);x['copy_rules'][0]['min_retain_seconds']=101;invalid.append(x)
        x=copy.deepcopy(data);x['copy_rules'][0]['clock_basis']='created_at';invalid.append(x)
        x=copy.deepcopy(data);x['copy_rules']=x['copy_rules'][1:];invalid.append(x)
        x=copy.deepcopy(data);x['copy_rules'].append(x['copy_rules'][0]);invalid.append(x)
        x=copy.deepcopy(data);x['schema_version']=1;invalid.append(x)
        x=copy.deepcopy(data);x['approved_by']=str(uuid4());invalid.append(x)
        for x in invalid:
            require((await post(x)).status_code==400,'effect-input-closed')
            # Independent runtime SQL caller cannot bypass application validation.
            await headers();x['csrf_hash']=digest('csrf',self.people[op]['csrf'])
            async with self.database.transaction() as connection:
                result=(await (await connection.execute('SELECT '+self.schema+'.privacy_effect_policy_call(%s,\'publish\',%s)',(digest('session',self.people[op]['token']),Jsonb(x)))).fetchone())[0]
            require(result['status']=='invalid','effect-SQL-validation')
        require(await self.scalar('SELECT count(*) FROM {s}.privacy_effect_policies')==1,'effect-invalid-no-rows')
        self.passed('both_HTTP_and_restricted_SQL_reject_missing_copyclass_coverage_invalid_minmax_clock_duplicate_and_forged_approval')
        altered=copy.deepcopy(data);altered['copy_rules'][-1]['expire_after_seconds']=61
        require((await post(altered)).status_code==409,'effect-altered-artifact-replay')
        stale=payload();require((await post(stale)).status_code==409,'effect-stale-revision')
        cross=await post(payload(),foreign);require(cross.status_code==201 and cross.json()['policy']['tenant_id']==str(self.people[foreign]['tenant']),'effect-other-tenant-local')
        require((await get()).json()['policy']==record,'effect-foreign-unchanged')
        self.passed('artifact_TTL_is_receipt_bound_and_same_key_mutation_stale_CAS_and_cross_tenant_override_fail')
        expired=copy.deepcopy(record);expired.update(revision=2,approved_at=int(time.time())-20,expires_at=int(time.time())-10)
        await self.execute('INSERT INTO {s}.privacy_effect_policies(tenant_id,revision,policy,approved_by,approved_at) VALUES(%s,2,%s,%s,clock_timestamp()-interval \'20 seconds\')',(self.people[op]['tenant'],Jsonb(expired),self.people[op]['person']))
        current=await get();require(current.status_code==200 and current.json()['policy']['revision']==2 and current.json()['effective'] is False,'expired-effect-current')
        async with self.database.transaction() as connection:
            row=(await (await connection.execute('SELECT '+self.schema+'.privacy_effect_policy_call(%s,\'effective\',\'{}\'::jsonb)',(digest('session',self.people[op]['token']),))).fetchone())[0]
        require(row['status']=='held','expired-effect-no-fallback')
        self.passed('expired_latest_v2_policy_denies_effects_without_falling_back_to_v1_or_older_v2')


def main():
    logging.disable(logging.CRITICAL);p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');args=p.parse_args()
    if not args.live:p.error('Explicit --live required for owned disposable fixture.')
    folder=ROOT/'local/data01/effect-registry-actual';folder.mkdir(parents=True,exist_ok=True);report=dict(passed=False)
    files=list((ROOT/'backend/src').rglob('*.py'))+list((ROOT/'backend/src').rglob('*.sql'))+[Path(__file__),ROOT/'scripts/checks/check_privacy_registry.py']
    hashes={f.relative_to(ROOT).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
    try:
        settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:report=runner.run(verify(direct,base,harness_type=Harness,target_version=33,operation='privacy_registry',direct_timeout=240))
        require(all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h for n,h in hashes.items()),'effect-registry-source-stable')
        report.pop('ingestion',None);report.update(source_sha256=hashes,scope='Actual configured33 v2 policy approval and receipt/API/SQL validation. All retention values synthetic fixtures; no storage effects/export artifact/provider or independentjournal proof.')
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
