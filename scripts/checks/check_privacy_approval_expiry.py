"""Deterministic post-lock expiry delta for v1 metadata and v2 approval."""
import argparse,asyncio,hashlib,json,logging,time,traceback
from pathlib import Path
from uuid import uuid4
from psycopg.types.json import Jsonb
from check_privacy_registry import Harness as RegistryHarness
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


class Harness(RegistryHarness):
    async def cases(self,browser,resources,op,foreign,wrong_scope):
        person=self.people[op];tenant=person['tenant']
        async def headers():
            cookie={'cookie':'__Host-eai-session='+person['token']}
            me=await browser.get('/api/v1/me',headers=dict(cookie,**{'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'}));require(me.status_code==200,'expiry-bootstrap');person['csrf']=me.json()['csrf_token']
            return dict(cookie,origin=ORIGIN,**{'content-type':'application/json','x-csrf-token':person['csrf'],'x-eai-expected-context':me.json()['context_binding']})
        legacy=dict(key=str(uuid4()),revision=1,expires_at=int(time.time())+3600,rules=[dict(category=c.value,retention_seconds=10,regions=['ap-southeast-1'],stores=sorted(REQUIRED[c]),provider_retention_seconds=0,training_allowed=False) for c in Category])
        for effect,seed in [(False,legacy),(True,effect_payload())]:
            table='privacy_effect_policies' if effect else 'privacy_policies';decisions='privacy_effect_decisions' if effect else 'privacy_policy_decisions'
            function='privacy_effect_policy_call' if effect else 'privacy_policy_call';path='/api/v1/operator/privacy-effect-policy' if effect else '/api/v1/operator/privacy-policy'
            setup=await browser.post(path,json=seed,headers=await headers());require(setup.status_code==201,'expiry-baseline-setup');prior=setup.json()['policy']
            data=dict(seed,key=str(uuid4()),revision=2);data['csrf_hash']=digest('csrf',person['csrf']);task=None
            async def publish():
                async with self.database.transaction() as connection:
                    return (await (await connection.execute('SELECT '+self.schema+'.'+function+'(%s,\'publish\',%s)',(digest('session',person['token']),Jsonb(data)))).fetchone())[0]
            try:
                async with self.admin.transaction():
                    await self.execute("SELECT pg_advisory_xact_lock(hashtextextended('authorization-role:'||%s::text,0))",(tenant,))
                    data['expires_at']=float(await self.scalar('SELECT extract(epoch FROM clock_timestamp())'))+2
                    task=asyncio.create_task(publish());waiting=False
                    for _ in range(30):
                        await self.execute('SELECT pg_stat_clear_snapshot()')
                        waiting=await self.scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=%s AND wait_event_type='Lock' AND query LIKE %s)",('eai_runtime_'+self.schema.rsplit('_',1)[1],'%'+function+'%'))
                        if waiting:break
                        await asyncio.sleep(.03)
                    require(waiting,'expiry-role-lock-observed')
                    remaining=data['expires_at']-float(await self.scalar('SELECT extract(epoch FROM clock_timestamp())'))
                    if remaining>0:await asyncio.sleep(remaining+.03)
                    require(float(await self.scalar('SELECT extract(epoch FROM clock_timestamp())'))>=data['expires_at'],'expiry-crossed-before-release')
                result=await task;require(result['status']=='invalid','expired-wait-no-approval')
            finally:
                if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
            require(await self.scalar('SELECT count(*) FROM {s}.'+table+' WHERE tenant_id=%s',(tenant,))==1,'no-expired-policy-row')
            require(await self.scalar('SELECT count(*) FROM {s}.'+decisions+' WHERE tenant_id=%s',(tenant,))==1,'no-expired-receipt-row')
            current=await browser.get(path,headers=await headers());require(current.status_code==200 and current.json()['policy']==prior and current.json()['effective'] is True,'prior-policy-still-readable')
            self.passed(('v2' if effect else 'v1')+'_actual_wait_crossing_expiry_rejects_policy_and_receipt_and_preserves_previous_current')


def main():
    logging.disable(logging.CRITICAL);p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');a=p.parse_args()
    if not a.live:p.error('Explicit --live required for owned disposable fixture.')
    folder=ROOT/'local/data01/approval-expiry-delta';folder.mkdir(parents=True,exist_ok=True);result=dict(passed=False)
    files=list((ROOT/'backend/src').rglob('*.py'))+list((ROOT/'backend/src').rglob('*.sql'))+[Path(__file__),ROOT/'scripts/checks/check_privacy_effect_registry.py',ROOT/'scripts/checks/check_privacy_registry.py'];hashes={f.relative_to(ROOT).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
    try:
        settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=34,operation='privacy_registry',direct_timeout=240))
        require(all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h for n,h in hashes.items()),'expiry-source-stable')
        result.pop('ingestion',None);result.update(source_sha256=hashes,scope='Deterministic post-lock expiry delta only, v1/v2 actual restricted RPC. Prior approval tests reused; no erasure/export/provider or real business policy approval.')
    except Exception as error:
        result.update(error_type=type(error).__name__,check=str(error) if isinstance(error,CheckFailure) else 'sanitized_failure',frames=[dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(error.__traceback__)])
    (folder/'database-proof.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}));raise SystemExit(0 if result['passed'] else 1)


if __name__=='__main__':main()
