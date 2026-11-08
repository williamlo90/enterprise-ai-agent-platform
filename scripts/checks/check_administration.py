"""BD-FE06 actual composed admin/browser/DB proof, disposable schema only."""
from check_policy_draft import *
from check_policy_draft import Harness as PolicyHarness
import traceback
import copy
import secrets
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from cryptography.fernet import Fernet
from enterprise_ai_platform import composition as wiring
from enterprise_ai_platform import main as platform_main
from enterprise_ai_platform.infrastructure.database.administration import PostgresAdministrationRepository
from enterprise_ai_platform.infrastructure.database import migration as migrations
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import access_role
from enterprise_ai_platform.file_check import ORIGIN
from uuid import UUID,uuid4
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from check_knowledge_workspace import verify

ROOT=Path(__file__).resolve().parents[2];DIAGNOSTICS=[]
class OwnedIO:
    def __init__(self,db):self.database=db;self.events=[]
    def __getattr__(self,key):return getattr(self.database,key)
    async def open(self):self.events.append('open')
    async def close(self):self.events.append('close')

class Harness(PolicyHarness):
    def passed(self,name):super().passed(name);print(json.dumps(dict(check=name,passed=True)),flush=True)
    async def administration(self):
        await self.policy_kernel()
        tenant=self.people[0]['tenant'];admin_index=3
        # Explicit external bootstrap fixture: no browser API can grant operator.
        for scopes in [('provisioning','job_metadata'),('provisioning',)]:
            person=uuid4();member=await self.member(tenant,person,grants=tuple(RoleGrant('operator_scoped','operations',s) for s in scopes));token,csrf=await self.session(tenant,person)
            self.people.append(dict(tenant=tenant,person=person,member=member,token=token,csrf=csrf))
        operator=len(self.people)-2;other_operator=len(self.people)-1
        issuer='https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_Test';domain='https://fixture.auth.ap-southeast-1.amazoncognito.com'
        settings=Settings(env='test',host='127.0.0.1',port=8000,database_enabled=True,database_url='postgresql://runtime:fixture@db.example.test/platform',auth_enabled=True,auth_callback_logging_controlled=True,auth_jwks_background_refresh=False,auth_verifier_key=Fernet.generate_key().decode(),oidc_issuer=issuer,oidc_client_id='fixture',oidc_client_secret='fixture-secret',public_origin=ORIGIN,oidc_redirect_uri=ORIGIN+'/api/v1/auth/callback',oidc_authorization_endpoint=domain+'/oauth2/authorize',oidc_token_endpoint=domain+'/oauth2/token',oidc_jwks_uri=issuer+'/.well-known/jwks.json',browser_context_enabled=True,administration_enabled=True,knowledge_enabled=True,conversations_enabled=True,organization_enabled=True)
        io=OwnedIO(self.database)
        original=PostgresAdministrationRepository.call
        async def traced(repo,*args,**kwargs):
            try:return await original(repo,*args,**kwargs)
            except Exception as error:
                current=error
                for _ in range(6):
                    if current is None:break
                    DIAGNOSTICS.append(dict(type=type(current).__name__,sqlstate=getattr(current,'sqlstate',None),frames=[dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(current.__traceback__)]));current=current.__context__
                raise
        with patch.object(wiring,'RuntimeDatabase',lambda *a,**kw:io):resources=wiring.build_resources(settings)
        with patch.object(platform_main,'build_resources',lambda _:resources):app=platform_main.create_app(settings)
        with patch.object(PostgresAdministrationRepository,'call',traced):
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as browser:await self.cases(browser,resources,tenant,admin_index,operator,other_operator)
        require(io.events==['open','close'],'admin-owned-lifecycle')
        self.passed('actual_configured_administration_feature_and_G13_identity_services_close_owned_lifecycle_without_provider_or_cloud_calls')

    async def cases(self,browser,resources,tenant,ad,op,op2):
        async def headers(index=ad):
            p=self.people[index];cookies={'cookie':'__Host-eai-session='+p['token']}
            me=await browser.get('/api/v1/me',headers={**cookies,'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'});require(me.status_code==200,'admin-bootstrap-'+str(me.status_code));p['csrf']=me.json()['csrf_token']
            return {**cookies,'origin':ORIGIN,'content-type':'application/json','x-csrf-token':p['csrf'],'x-eai-expected-context':me.json()['context_binding']}
        async def get(path,index=ad):return await browser.get('/api/v1/'+path,headers=await headers(index))
        async def post(path,body,index=ad):return await browser.post('/api/v1/'+path,json=body,headers=await headers(index))
        async def patcher(path,body,index=ad):return await browser.patch('/api/v1/'+path,json=body,headers=await headers(index))
        async def membership(id):
            r=await get('admin/memberships/'+str(id));require(r.status_code==200,'admin-member-read');return r.json()['member']
        members=await get('admin/memberships?limit=1');require(members.status_code==200 and members.json()['next_after'] is not None,'members-catalog-'+str(members.status_code))
        require((await get('admin/memberships?limit=1&after='+members.json()['next_after'])).status_code==200,'members-next-page')
        for index in (0,2,op):require((await get('admin/memberships',index)).status_code==403,'nonadmin-no-directory')
        require((await get('admin/memberships/'+str(self.people[2]['member']))).status_code==404,'foreign-membership-hidden')
        source=self.policy_fixture['source']
        require((await get('knowledge/documents/'+source['document_id'])).status_code==404,'admin-no-knowledge-content')
        require((await get('conversations')).status_code==403,'admin-no-chat-content')
        self.passed('actual_admin_directory_pagination_is_tenant_scoped_employee_foreign_and_operator_cannot_enumerate_members_or_read_content')
        config=(await get('admin/config')).json();require(config['config_version']==1,'initial-config')
        catalog=await get('admin/config/catalog');require(catalog.status_code==200 and set(catalog.json())=={'connectors','model_policies','replayed'},'registered-ref-metadata')
        changed=dict(assistant_name='Fictional Assistant 2',locales=['id','en'],help_contact='Hubungi HR resmi',instruction_text='Baris satu\nBaris dua\t tetap data.',connector_ref='reference-reimbursement',model_policy_ref='nova-pro-apac-fictional')
        command=dict(expected_version=1,key=str(uuid4()),config=changed)
        r=await patcher('admin/config',command);require(r.status_code==200 and r.json()['config_version']==2,'config-CAS-'+str(r.status_code))
        require((await patcher('admin/config',command)).json()['replayed'] is True,'config-replay')
        mismatch=copy.deepcopy(command);mismatch['config']['assistant_name']='Different'
        require((await patcher('admin/config',mismatch)).status_code==409,'config-key-material-conflict')
        stale=copy.deepcopy(command);stale['key']=str(uuid4());require((await patcher('admin/config',stale)).status_code==409,'config-stale-version')
        evil=copy.deepcopy(command);evil.update(expected_version=2,key=str(uuid4()));evil['config']['connector_ref']='https://attacker.test';require((await patcher('admin/config',evil)).status_code==400,'no-arbitrary-endpoint')
        evil['config']['connector_ref']='unregistered';require((await patcher('admin/config',evil)).status_code==400,'no-unregistered-ref')
        profile=await get('tenant/profile',0);require(profile.status_code==200 and set(profile.json())=={'config_version','assistant_name','locales','help_contact','replayed'} and profile.json()['assistant_name']==changed['assistant_name'],'safe-profile-current')
        require((await get('admin/config',op)).status_code==403,'operator-no-private-instructions')
        self.passed('immutable_config_version_key_material_and_registered_refs_enforced_safe_member_profile_observes_current_presentation_without_exposing_private_instruction_or_runtime_routing_claim')
        own=await membership(self.people[ad]['member'])
        role=dict(expected_version=own['version'],key=str(uuid4()),operation='grant',role='employee',scope_kind='tenant',scope_ref=str(tenant))
        require((await post('admin/memberships/'+str(own['id'])+'/roles',role)).status_code==403,'self-role-denied')
        role['role']='operator_scoped';require((await post('admin/memberships/'+str(self.people[1]['member'])+'/roles',role)).status_code==400,'operator-role-not-browser-grant')
        person=self.people[1];m=await membership(person['member']);old=await headers(1)
        role=dict(expected_version=m['version'],key=str(uuid4()),operation='grant',role='approver',scope_kind='process',scope_ref='reimbursement')
        r=await post('admin/memberships/'+str(m['id'])+'/roles',role);require(r.status_code==200,'role-grant-'+str(r.status_code))
        require((await post('admin/memberships/'+str(m['id'])+'/roles',role)).json()['replayed'] is True,'role-stable-replay')
        require((await browser.get('/api/v1/tenant/profile',headers=old)).status_code in {401,409},'old-role-session-invalid')
        person['token'],person['csrf']=await self.session(tenant,person['person'])
        require((await get('tenant/profile',1)).status_code==200,'new-role-session-current')
        self.passed('role_browser_bridge_uses_actual_AUTH03_CAS_scope_anti_self_and_audited_replay_without_operator_grant_old_original_session_is_invalidated')
        # Link an existing principal from another tenant, never create identity.
        target=self.people[2];before=await self.scalar('SELECT count(*) FROM {s}.principals')
        command=dict(principal_id=str(target['person']),key=str(uuid4()))
        r=await post('admin/memberships',command);require(r.status_code==201,'link-registered-principal');mid=r.json()['membership_id']
        require((await post('admin/memberships',command)).json()['membership_id']==mid and await self.scalar('SELECT count(*) FROM {s}.principals')==before,'membership-replay-no-provider-identity')
        linked_token,linked_csrf=await self.session(tenant,target['person']);self.people.append(dict(tenant=tenant,person=target['person'],member=UUID(mid),token=linked_token,csrf=linked_csrf));li=len(self.people)-1;old=await headers(li)
        m=await membership(mid);command=dict(expected_version=m['version'],active=False,key=str(uuid4()))
        r=await patcher('admin/memberships/'+mid,command);require(r.status_code==200,'disable-membership')
        require((await browser.get('/api/v1/tenant/profile',headers=old)).status_code==401,'disabled-original-session')
        m=await membership(mid);require((await patcher('admin/memberships/'+mid,dict(expected_version=m['version'],active=True,key=str(uuid4())))).status_code==200,'reenable-member')
        require((await browser.get('/api/v1/tenant/profile',headers=old)).status_code==401,'reenable-no-session-revival')
        require((await get('tenant/profile',2)).status_code==200,'other-tenant-session-unaffected')
        require((await patcher('admin/memberships/'+str(own['id']),dict(expected_version=own['version'],active=False,key=str(uuid4())))).status_code==403,'self-disable-denied')
        self.passed('membership_admission_links_existing_identity_and_lifecycle_invalidates_exact_tenant_sessions_disable_reenable_never_revives_old_cookie_or_changes_other_tenant')
        pid=self.policy_fixture['policy']['id'];r=await get('admin/policies/'+pid+'/versions/1');require(r.status_code==200 and r.json()['canonical'] is None,'admin-safe-policy-metadata')
        r=await get('admin/policies/'+pid+'/versions/1',0);require(r.status_code==200 and json.loads(r.json()['canonical'])['rules']==self.policy_fixture['content']['rules'],'process-owner-rules-not-docbody')
        require((await get('admin/policies',op)).status_code==403,'operator-no-policy-rules')
        require((await get('admin/scopes')).status_code==200,'scoped-role-catalog')
        self.passed('policy_status_metadata_and_process_owner_canonical_rule_snapshot_are_scoped_source_document_content_and_operator_policy_access_are_not_granted')
        jobs=await get('operator/jobs',op);require(jobs.status_code==200,'operator-job-scope')
        for row in jobs.json()['items']:require(set(row)=={'id','job_type','state','attempts','retry_limit','error_code','next_attempt_at'},'job-metadata-allowlist')
        require((await get('operator/jobs',op2)).status_code==403,'provisioning-not-job-scope')
        require((await get('operator/tenants')).status_code==403,'admin-cannot-provision')
        self.passed('operator_job_metadata_is_current_tenant_allowlisted_without_target_payload_or_replay_authority_and_separate_from_provisioning_scope')
        # Deferred audit failure must roll back business writes and replay keys.
        await self.execute("CREATE FUNCTION {s}._admin_fixture_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Controlled audit failure' USING ERRCODE='40001';END $$")
        await self.admin.execute(sql.SQL('REVOKE ALL ON FUNCTION {}._admin_fixture_failure() FROM PUBLIC,{}').format(sql.Identifier(self.schema),sql.Identifier(access_role(self.schema))))
        await self.execute('CREATE CONSTRAINT TRIGGER admin_fixture_failure AFTER INSERT ON {s}.admin_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {s}._admin_fixture_failure()')
        failed=dict(expected_version=2,key=str(uuid4()),config={**changed,'assistant_name':'Must roll back'})
        provision=dict(slug='fixture-'+uuid4().hex,name='Fictional provisioned tenant',timezone='Asia/Singapore',initial_admin_id=str(self.people[1]['person']),key=str(uuid4()))
        count=await self.scalar('SELECT count(*) FROM {s}.tenants');requests=await self.scalar('SELECT count(*) FROM {s}.admin_requests')
        try:
            require((await patcher('admin/config',failed)).status_code==503,'audit-config-failure')
            require((await post('operator/tenants',provision,op)).status_code==503,'audit-provision-failure')
            require((await get('admin/config')).json()['config_version']==2,'audit-config-rollback')
            require(await self.scalar('SELECT count(*) FROM {s}.tenants')==count and await self.scalar('SELECT count(*) FROM {s}.admin_requests')==requests,'audit-header-snapshot-membership-journal-rollback')
        finally:
            await self.execute('DROP TRIGGER admin_fixture_failure ON {s}.admin_events');await self.execute('DROP FUNCTION {s}._admin_fixture_failure()')
        self.passed('deferred_audit_failure_rolls_back_config_snapshot_and_operator_first_tenant_config_membership_role_and_journal_in_actual_owner_transaction')
        r=await post('operator/tenants',provision,op);require(r.status_code==201,'operator-provision-'+str(r.status_code));newtenant=r.json()['tenant_id']
        require(r.json()['integration_state']=='requires_service_registration','not-fake-service-ready')
        require((await post('operator/tenants',provision,op)).json()['tenant_id']==newtenant,'provision-stable-replay')
        evil={**provision,'key':str(uuid4()),'initial_admin_id':str(self.people[op]['person'])};require((await post('operator/tenants',evil,op)).status_code==403,'operator-no-self-bootstrap')
        rows=(await get('operator/tenants',op)).json()['items'];require(newtenant in {row['id'] for row in rows},'own-created-tenant-metadata')
        require(newtenant not in {row['id'] for row in (await get('operator/tenants',op2)).json()['items']},'foreign-provisioning-not-visible')
        require(await self.scalar('SELECT count(*) FROM {s}.memberships WHERE tenant_id=%s AND principal_id=%s',(newtenant,self.people[op]['person']))==0,'operator-no-target-membership')
        require(await self.scalar('SELECT config_version FROM {s}.tenants WHERE id=%s',(newtenant,))==1 and await self.scalar('SELECT count(*) FROM {s}.tenant_configs WHERE tenant_id=%s',(newtenant,))==1,'initial-deferred-config-pair-valid')
        self.passed('operator_provisioning_atomically_creates_deferred_initial_config_and_distinct_registered_admin_replays_same_tenant_exposes_only_own_metadata_and_never_grants_operator_target_membership')
        # The new tenant admin can read config, but does not acquire employee/KM.
        adminperson=self.people[1]['person'];token,csrf=await self.session(UUID(newtenant),adminperson);self.people.append(dict(tenant=UUID(newtenant),person=adminperson,member=UUID(r.json()['initial_admin_membership_id']),token=token,csrf=csrf));ni=len(self.people)-1
        require((await get('admin/config',ni)).status_code==200 and (await get('conversations',ni)).status_code==403,'new-admin-not-content-role')
        require((await get('admin/memberships/'+str(self.people[ad]['member']),ni)).status_code==404,'newtenant-cannot-read-anchor-admin')
        audit=await get('admin/audit');require(audit.status_code==200 and audit.json()['items'],'audit-visible')
        for row in audit.json()['items']:require(set(row)=={'id','actor_id','operation','object_id','object_version','created_at'},'audit-no-config-content')
        self.passed('new_tenant_admin_isolated_from_anchor_memberships_and_content_roles_audit_contains_only_authorized_immutable_operation_metadata')
        # Two HTTP requests in one bounded validation/pool2, never parallel files.
        same_headers=await headers();before=await self.scalar('SELECT count(*) FROM {s}.admin_requests')
        commands=[dict(expected_version=2,key=str(uuid4()),config={**changed,'assistant_name':'Race '+name}) for name in ('A','B')]
        responses=await asyncio.gather(*(browser.patch('/api/v1/admin/config',json=value,headers=same_headers) for value in commands))
        require(sorted(r.status_code for r in responses)==[200,409],'actual-concurrent-config-CAS-'+str([r.status_code for r in responses]))
        require((await get('admin/config')).json()['config_version']==3 and await self.scalar('SELECT count(*) FROM {s}.admin_requests')==before+1,'one-config-winner-audit')
        stale_headers={**(await headers()),'x-eai-expected-context':'0'*64}
        blocked=await browser.patch('/api/v1/admin/config',json=dict(expected_version=3,key=str(uuid4()),config=changed),headers=stale_headers)
        require(blocked.status_code==409 and (await get('admin/config')).json()['config_version']==3,'expected-context-denied-before-write')
        self.passed('two_request_config_race_serializes_before_original_session_G13_locks_one_CAS_winner_one409_and_stale_expected_context_cannot_commit_after_lock_only_phase')



def main():
    logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned fixtures.')
    folder=ROOT/'local/bdfe06';folder.mkdir(parents=True,exist_ok=True);report={'passed':False}
    try:
        files=list((ROOT/'backend/src').rglob('*.py'))+list((ROOT/'backend/src').rglob('*.sql'))+list((ROOT/'reference_service/src').rglob('*.py'))+list((ROOT/'reference_service/src').rglob('*.sql'))+list((ROOT/'scripts/checks').glob('*.py'))
        hashes={str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:report=runner.run(verify(direct,base,harness_type=Harness,target_version=29,operation='administration',direct_timeout=500))
        require(all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h for n,h in hashes.items()),'administration-source-stable')
        report.update(source_sha256=hashes,scope='actual configured platform29 administration/session/G13/AUTH03/POL metadata/CAS/audit/atomic provisioning in owned DB; external identity authentication and ingestion fixtures; no UI/cloud/provider acceptance')
    except Exception as error:
        report.update(error_type=type(error).__name__)
        if isinstance(error,CheckFailure):report['check']=str(error)
        report['diagnostics']=DIAGNOSTICS[-12:];frames=[];current=error
        for _ in range(8):
            if current is None:break
            frames.extend(dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(current.__traceback__))
            if getattr(current,'sqlstate',None):report['sqlstate']=current.sqlstate
            current=current.__context__
        report['frames']=frames
    (folder/'administration-proof.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}));raise SystemExit(0 if report['passed'] else 1)
if __name__=='__main__':main()
