"""One owned actual platform31 registry proof; no AWS/provider or installed upgrade."""
import argparse,asyncio,copy,hashlib,json,logging,time,traceback
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import httpx
from cryptography.fernet import Fernet
from psycopg.types.json import Jsonb
from check_knowledge_workspace import Harness as WorkspaceHarness,verify
from check_administration import OwnedIO
from enterprise_ai_platform import composition as wiring,main as platform_main
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database import privacy_registry
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.modules.identity.service import digest
from enterprise_ai_platform.modules.privacy.domain import Category,REQUIRED

ROOT=Path(__file__).resolve().parents[2]
PHASE='full'


class Harness(WorkspaceHarness):
    def passed(self,name):
        super().passed(name);print(json.dumps(dict(check=name,passed=True)),flush=True)

    async def privacy_registry(self):
        tenant=self.people[0]['tenant'];indices=[]
        for t,scope in [(tenant,'config'),(self.people[2]['tenant'],'config'),(tenant,'provisioning')]:
            person=uuid4();member=await self.member(t,person,grants=(RoleGrant('operator_scoped','operations',scope),));token,csrf=await self.session(t,person)
            indices.append(len(self.people));self.people.append(dict(tenant=t,person=person,member=member,token=token,csrf=csrf))
        op,foreign,wrong_scope=indices
        issuer='https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_Test';domain='https://fixture.auth.ap-southeast-1.amazoncognito.com'
        settings=Settings(env='test',host='127.0.0.1',port=8000,database_enabled=True,database_url='postgresql://runtime:fixture@db.example.test/platform',auth_enabled=True,auth_callback_logging_controlled=True,auth_jwks_background_refresh=False,auth_verifier_key=Fernet.generate_key().decode(),oidc_issuer=issuer,oidc_client_id='fixture',oidc_client_secret='fixture-secret',public_origin=ORIGIN,oidc_redirect_uri=ORIGIN+'/api/v1/auth/callback',oidc_authorization_endpoint=domain+'/oauth2/authorize',oidc_token_endpoint=domain+'/oauth2/token',oidc_jwks_uri=issuer+'/.well-known/jwks.json',browser_context_enabled=True,privacy_enabled=True)
        io=OwnedIO(self.database)
        with patch.object(wiring,'RuntimeDatabase',lambda *a,**k:io):resources=wiring.build_resources(settings)
        with patch.object(platform_main,'build_resources',lambda _:resources):app=platform_main.create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as browser:
                await self.cases(browser,resources,op,foreign,wrong_scope)
        require(io.events==['open','close'],'privacy-owned-lifecycle')
        self.passed('configured_actual_privacy_registry_identity_G13_and_owned_resource_lifecycle')

    async def cases(self,browser,resources,op,foreign,wrong_scope):
        tenant=self.people[op]['tenant'];path='/api/v1/operator/privacy-policy'
        async def headers(index=op):
            p=self.people[index];cookies={'cookie':'__Host-eai-session='+p['token']}
            me=await browser.get('/api/v1/me',headers=dict(cookies,**{'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'}));require(me.status_code==200,'privacy-bootstrap-'+str(me.status_code));p['csrf']=me.json()['csrf_token']
            return dict(cookies,origin=ORIGIN,**{'content-type':'application/json','x-csrf-token':p['csrf'],'x-eai-expected-context':me.json()['context_binding']})
        async def get(index=op):return await browser.get(path,headers=await headers(index))
        async def post(body,index=op):return await browser.post(path,json=body,headers=await headers(index))
        def payload(revision=1):return dict(key=str(uuid4()),revision=revision,expires_at=int(time.time())+3600,rules=[dict(category=c.value,retention_seconds=10,regions=['ap-southeast-1'],stores=sorted(REQUIRED[c]),provider_retention_seconds=0,training_allowed=False) for c in Category])
        if PHASE=='full':
            first=await get();require(first.status_code==200 and first.json()['policy'] is None,'empty-policy')
            for index in [0,3,4,wrong_scope]:require((await get(index)).status_code==403,'privacy-role-denied')
            require((await get(foreign)).json()['policy'] is None,'foreign-empty-scope')
            self.passed('empty_registry_has_no_default_and_wrong_roles_cannot_inspect_policy')
            data=payload();response=await post(data);require(response.status_code==201,'publish-'+str(response.status_code));value=response.json();record=value['policy']
            require(record['tenant_id']==str(tenant) and record['approved_by']==str(self.people[op]['person']) and record['revision']==1 and value['effective'] is True,'original-approval-identity')
            replay=await post(data);require(replay.status_code==201 and replay.json()['replayed'] is True and replay.json()['receipt']==value['receipt'] and replay.json()['policy']==record,'exact-replay')
            require(await self.scalar('SELECT count(*) FROM {s}.privacy_policy_decisions')==1 and await self.scalar('SELECT count(*) FROM {s}.privacy_policies')==1,'atomic-approval-count')
            require(response.headers.get('cache-control')=='no-store' and 'x-eai-context-binding' in response.headers,'privacy-headers')
            self.passed('actual_atomic_original_actor_approval_snapshot_receipt_and_exact_replay')
            for key in ['tenant_id','approved_by','approved_at','role']:
                forged=copy.deepcopy(data);forged[key]=str(uuid4());require((await post(forged)).status_code==400,'forged-'+key)
            changed=copy.deepcopy(data);changed['expires_at']+=1;require((await post(changed)).status_code==409,'changed-replay')
            require((await post(payload())).status_code==409,'stale-cas')
            missing=payload(2);missing['rules']=missing['rules'][1:];require((await post(missing)).status_code==400,'identity-required')
            require(await self.scalar('SELECT count(*) FROM {s}.privacy_policies')==1,'no-forged-state')
            for index in [0,3,wrong_scope]:require((await post(payload(2),index)).status_code==403,'write-role-denied')
            self.passed('forged_approval_tenant_wrong_scope_missing_identity_and_stale_CAS_do_not_publish')
            fresh=payload(2);foreign_result=await post(payload(),foreign);require(foreign_result.status_code==201 and foreign_result.json()['policy']['tenant_id']==str(self.people[foreign]['tenant']),'tenant-scoped-approval')
            require((await get()).json()['policy']['revision']==1,'foreign-no-mutation')
            self.passed('original_active_tenant_is_inferred_not_overridden_by_payload_or_other_tenant')
        else:
            data=payload();response=await post(data);require(response.status_code==201,'delta-baseline-setup');record=response.json()['policy'];fresh=payload(2)
        original=privacy_registry.after_read
        async def expire_inside_uow(database,connection,token,family):
            if family=='privacy':require((await (await connection.execute('SELECT '+self.schema+'.identity_call(\'logout\',%s)',(Jsonb(dict(token_hash=token,csrf_hash=digest('csrf',self.people[op]['csrf']))),))).fetchone())[0]['status']=='ok','injected-valid-logout')
            return await original(database,connection,token,family)
        with patch.object(privacy_registry,'after_read',expire_inside_uow):denied=await post(fresh)
        require(denied.status_code in (401,409) and await self.scalar('SELECT count(*) FROM {s}.privacy_policies WHERE tenant_id=%s',(tenant,))==1,'atomic-final-guard-rollback')
        self.passed('injected_logout_before_UoW_final_guard_rolls_back_snapshot_and_approval_receipt')
        stale_headers=await headers();race=None
        try:
            async with self.admin.transaction():
                await self.execute("SELECT pg_advisory_xact_lock(hashtextextended('authorization-role:'||%s::text,0))",(tenant,))
                await self.execute('UPDATE {s}.memberships SET access_revision=access_revision+1,version=version+1 WHERE id=%s',(self.people[op]['member'],))
                race=asyncio.create_task(browser.post(path,json=fresh,headers=stale_headers))
                await asyncio.sleep(.2)
            rejected=await race;require(rejected.status_code in (401,409),'revoked-publish-'+str(rejected.status_code))
        finally:
            if race is not None and not race.done():race.cancel();await asyncio.gather(race,return_exceptions=True)
        require(await self.scalar('SELECT count(*) FROM {s}.privacy_policies WHERE tenant_id=%s',(tenant,))==1,'revocation-no-policy')
        self.passed('actual_role_writer_overlap_rejects_original_stale_session_without_new_approval')
        self.people[op]['token'],self.people[op]['csrf']=await self.session(tenant,self.people[op]['person'])
        expired=copy.deepcopy(record);expired.update(revision=2,approved_at=int(time.time())-20,expires_at=int(time.time())-10)
        await self.execute('INSERT INTO {s}.privacy_policies(tenant_id,revision,policy,approved_by,approved_at) VALUES(%s,2,%s,%s,clock_timestamp()-interval \'20 seconds\')',(tenant,Jsonb(expired),self.people[op]['person']))
        latest=await get();require(latest.status_code==200 and latest.json()['policy']['revision']==2 and latest.json()['effective'] is False,'expired-latest-visible')
        async with self.database.transaction() as connection:
            row=await (await connection.execute('SELECT '+self.schema+'.privacy_policy_call(%s,\'effective\',\'{}\'::jsonb)',(digest('session',self.people[op]['token']),))).fetchone()
        require(row[0]['status']=='held','no-old-fallback')
        self.passed('expired_latest_policy_never_falls_back_to_older_active_policy_for_processing')


def main():
    global PHASE
    logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');parser.add_argument('--phase',choices=['full','delta'],default='full');a=parser.parse_args();PHASE=a.phase
    if not a.live:parser.error('Explicit --live required for owned disposable fixture.')
    folder=ROOT/('local/data01/registry-actual' if PHASE=='full' else 'local/data01/registry-delta');folder.mkdir(parents=True,exist_ok=True);report=dict(passed=False)
    files=list((ROOT/'backend/src').rglob('*.py'))+list((ROOT/'backend/src').rglob('*.sql'))+[Path(__file__)]
    hashes={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    try:
        settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:report=runner.run(verify(direct,base,harness_type=Harness,target_version=31,operation='privacy_registry',direct_timeout=240))
        require(all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h for n,h in hashes.items()),'privacy-source-stable')
        report.update(source_sha256=hashes,scope='Actual configured platform31 policy approval/session/G13/CSRF/tenant/CAS/receipt, source expiry and role-overlap; identity provider controlled. Not data erasure/export or full DATA01.')
    except Exception as error:
        report.update(error_type=type(error).__name__,check=str(error) if isinstance(error,CheckFailure) else 'sanitized_failure',frames=[dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(error.__traceback__)])
        chain=[];current=error
        for _ in range(8):
            if current is None:break
            entry=dict(type=type(current).__name__,sqlstate=getattr(current,'sqlstate',None))
            if getattr(current,'sqlstate',None)=='42601':entry['syntax']=getattr(getattr(current,'diag',None),'message_primary','syntax rejected')
            chain.append(entry);current=current.__context__
        report['diagnostics']=chain
    (folder/'database-proof.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8');print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}));raise SystemExit(0 if report['passed'] else 1)


if __name__=='__main__':main()
