"""BD-FE-01 scoped directory/admin form proof in owned disposable PostgreSQL."""
import argparse
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import UUID,uuid4
import httpx
from psycopg import sql
from check_knowledge_workspace import verify,Harness as WorkspaceHarness
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import ApplicationServices,database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.organization import PostgresOrganizationRepository
from enterprise_ai_platform.infrastructure.database.identity import PostgresIdentityRepository
from enterprise_ai_platform.knowledge_check import write_json
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.organization.service import OrganizationService
from enterprise_ai_platform.modules.identity.service import IdentityService,digest
from enterprise_ai_platform.modules.authorization.domain import RoleGrant


class Harness(WorkspaceHarness):
 async def client(self):
  services=ApplicationServices(health=SimpleNamespace(),files=self.files,knowledge=self.knowledge,organization=self.organization)
  return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=services)),base_url=ORIGIN)
 async def org_get(self,path,index=3):
  async with await self.client() as client:return await client.get('/api/v1/'+path,headers=self.headers(index))
 async def patch(self,mid,values,index=3):
  async with await self.client() as client:return await client.patch('/api/v1/organization/members/'+str(mid),json=values,headers=self.headers(index,'application/json'))
 def material(self,**changes):return dict(display_name='Budi',department='Finance',job_title='Manager',groups=['Travel','Finance'],expected_version=1,key=str(uuid4()),**changes)
 async def organization_cases(self):
  self.organization=OrganizationService(PostgresOrganizationRepository(self.database),ORIGIN)
  tenant=self.people[0]['tenant'];mid=self.people[0]['member'];pid=self.people[0]['person']
  before=await self.scalar('SELECT jsonb_build_array(access_revision,version) FROM {s}.memberships WHERE id=%s',(mid,))
  roles=await self.scalar('SELECT count(*) FROM {s}.membership_roles WHERE tenant_id=%s',(tenant,))
  detail=await self.org_get('organization/members/'+str(mid));require(detail.status_code==200,'member-'+str(detail.status_code))
  require(detail.json()['organization_version']==1 and detail.json()['local_display_name'] is None,'uninitialized-member')
  material=self.material();updated=await self.patch(mid,material);require(updated.status_code==200,'profile-'+str(updated.status_code));first=updated.json()
  require(first['organization_version']==2 and first['display_name']=='Budi' and first['groups']==['Finance','Travel'],'saved-name-groups')
  require((await self.patch(mid,material)).json()==first,'profile-exact-replay')
  require((await self.patch(mid,{**material,'groups':[' Finance ','Travel','Finance']})).json()==first,'normalized-group-replay')
  require((await self.patch(mid,{**material,'display_name':'changed'})).status_code==409,'changed-key')
  require((await self.patch(mid,{**material,'key':str(uuid4())})).status_code==409,'stale-profile')
  require(await self.scalar('SELECT jsonb_build_array(access_revision,version) FROM {s}.memberships WHERE id=%s',(mid,))==before,'membership-unchanged')
  require(await self.scalar('SELECT count(*) FROM {s}.membership_roles WHERE tenant_id=%s',(tenant,))==roles,'roles-unchanged')
  self.passed('admin_real_form_produces_tenant_name_attributes_and_independent_cas_receipt_without_role_or_session_revision_changes')
  for index in (0,2,4):require((await self.patch(mid,material,index)).status_code==404,'nonadmin-profile-write')
  require((await self.org_get('organization/members',0)).status_code==403,'manager-not-admin')
  require((await self.org_get('knowledge/directory',3)).status_code==403,'admin-not-manager')
  require((await self.org_get('organization/members/'+str(self.people[2]['member']))).status_code==404,'foreign-member')
  self.passed('tenant_admin_and_knowledge_manager_authorities_stay_distinct_and_foreign_members_are_hidden')
  filtered=await self.org_get('knowledge/directory?department=Finance&job_title=Manager',0)
  require(filtered.status_code==200 and len(filtered.json()['items'])==1 and filtered.json()['items'][0]['principal_id']==str(pid),'and-directory')
  require((await self.org_get('knowledge/directory?q=%25',0)).json()['items']==[],'literal-query')
  facets=await self.org_get('knowledge/directory/facets?field=group&department=Finance',0)
  require(facets.status_code==200 and facets.json()['items']==[dict(value='Finance',count=1),dict(value='Travel',count=1)],'scoped-facets')
  page=(await self.org_get('knowledge/directory/facets?field=group&limit=1',0)).json();require(page['next_after']=='Finance','facet-cursor')
  require((await self.org_get('knowledge/directory/facets?field=group&limit=1&after=Finance',0)).json()['items']==[dict(value='Travel',count=1)],'facet-next')
  require((await self.org_get('knowledge/directory/facets?field=group&after=FOREIGN',0)).status_code==404,'foreign-facet-cursor')
  self.passed('picker_and_facets_share_literal_and_filters_counts_and_scoped_value_cursors')
  page=(await self.org_get('organization/members?limit=1')).json();require(page['next_after'] is not None,'member-cursor')
  next_page=(await self.org_get('organization/members?limit=1&after='+page['next_after'])).json()
  require(next_page['items'][0]['membership_id']!=page['items'][0]['membership_id'],'member-next')
  require((await self.org_get('organization/members?after='+str(self.people[2]['member']))).status_code==404,'foreign-cursor')
  self.passed('membership_uuid_pagination_is_scoped_and_does_not_repeat_first_page')
  await self.execute("UPDATE {s}.principals SET display_name='Canonical fictional name' WHERE id=%s",(pid,))
  second={**material,'key':str(uuid4()),'expected_version':2,'display_name':None,'groups':[]}
  changed=await self.patch(mid,second);require(changed.status_code==200 and changed.json()['display_name']=='Canonical fictional name' and changed.json()['local_display_name'] is None,'null-fallback')
  require((await self.patch(mid,material)).json()==first,'old-receipt-preserved')
  require((await self.org_get('organization/members/'+str(mid))).json()['organization_version']==3,'old-replay-does-not-write')
  alt=await self.member(self.people[2]['tenant'],pid,grants=(RoleGrant('employee','tenant',str(self.people[2]['tenant'])),))
  require(await self.scalar('SELECT display_name FROM {s}.principals WHERE id=%s',(pid,))=='Canonical fictional name','global-name-unchanged')
  require(await self.scalar('SELECT count(*) FROM {s}.organization_profiles WHERE membership_id=%s',(alt,))==0,'other-tenant-profile-unchanged')
  self.passed('clearing_local_name_falls_back_without_mutating_global_identity_or_another_tenant_and_old_replay_preserves_newer_profile')
  await self.execute('UPDATE {s}.principals SET display_name=%s WHERE id=%s',(' '*10000+'Canonical fictional name'+' '*10000,pid))
  bounded=(await self.org_get('organization/members/'+str(mid))).json()
  require(bounded['display_name']=='Canonical fictional name','bounded-legacy-fallback')
  self.passed('legacy_padded_canonical_name_is_normalized_for_bounded_display_without_rewriting_identity')
  admin_person=uuid4();admin_member=await self.member(tenant,admin_person,grants=(RoleGrant('tenant_admin','tenant',str(tenant)),));admin_token,admin_csrf=await self.session(tenant,admin_person)
  self.people.append(dict(tenant=tenant,person=admin_person,member=admin_member,token=admin_token,csrf=admin_csrf));other_admin=len(self.people)-1
  left={**material,'key':str(uuid4()),'expected_version':3,'display_name':'Admin A change'}
  right={**left,'key':str(uuid4()),'display_name':'Admin B change'}
  async with asyncio.timeout(5):race=await asyncio.gather(self.patch(mid,left),self.patch(mid,right,other_admin))
  require(sorted(value.status_code for value in race)==[200,409],'same-profile-cas-race')
  async with asyncio.timeout(5):different=await asyncio.gather(self.patch(admin_member,{**material,'key':str(uuid4())}),self.patch(self.people[3]['member'],{**material,'key':str(uuid4())},other_admin))
  require([value.status_code for value in different]==[200,200],'reciprocal-profile-no-membership-upgrade')
  self.passed('two_admins_same_profile_cas_has_one_winner_and_reciprocal_profiles_finish_without_membership_deadlock')
  expired_token,expired_csrf=await self.session(tenant,self.people[3]['person'])
  before_profile=(await self.org_get('organization/members/'+str(mid))).json()
  before_requests=await self.scalar('SELECT count(*) FROM {s}.organization_requests')
  await self.execute("WITH expiry AS(SELECT clock_timestamp()+interval '1500 milliseconds' t) UPDATE {s}.sessions SET authenticated_at=expiry.t-interval '8 hours',expires_at=expiry.t FROM expiry WHERE token_hash=%s",(digest('session',expired_token),))
  task=None
  async def expiring_write():
   async with await self.client() as client:
    return await client.patch('/api/v1/organization/members/'+str(mid),json={**material,'key':str(uuid4()),'expected_version':before_profile['organization_version']},headers={**self.headers(3,'application/json'),'cookie':'__Host-eai-session='+expired_token,'x-csrf-token':expired_csrf})
  try:
   async with self.admin.transaction():
    await self.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('eai:organization-profile:'+str(tenant)+':'+str(mid),))
    task=asyncio.create_task(expiring_write());waiting=False
    for _ in range(30):
     await self.execute('SELECT pg_stat_clear_snapshot()')
     waiting=await self.scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=%s AND wait_event_type='Lock')",('eai_runtime_'+self.schema.rsplit('_',1)[1],))
     if waiting:break
     await asyncio.sleep(.05)
    require(waiting,'organization-profile-lock-wait')
    await asyncio.sleep(1.6)
   expired=await task;require(expired.status_code in (401,503),'expired-profile-no-success')
  finally:
   if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
  require((await self.org_get('organization/members/'+str(mid))).json()==before_profile and await self.scalar('SELECT count(*) FROM {s}.organization_requests')==before_requests,'expired-profile-full-rollback')
  self.passed('expiry_after_actual_profile_lock_wait_rolls_back_profile_and_receipt')
  inactive=self.people[1]['member']
  require((await self.patch(inactive,{**self.material(),'display_name':'Inactive fictional','department':'Hidden dept'})).status_code==200,'inactive-name')
  await self.execute('UPDATE {s}.memberships SET active=false WHERE id=%s',(inactive,))
  require((await self.org_get('organization/members/'+str(inactive))).status_code==200,'admin-inactive-maintenance')
  require((await self.org_get('knowledge/directory?q=Inactive',0)).json()['items']==[],'picker-inactive-hidden')
  require(all(item['value']!='Hidden dept' for item in (await self.org_get('knowledge/directory/facets?field=department',0)).json()['items']),'picker-inactive-facet-hidden')
  self.passed('admin_maintenance_retains_inactive_members_while_picker_and_facets_exclude_them')
  a,b=uuid4(),uuid4()
  for collection in (a,b):await self.execute("INSERT INTO {s}.authorization_scopes(tenant_id,scope_kind,scope_ref) VALUES(%s,'knowledge_collection',%s)",(tenant,collection))
  person=uuid4();member=await self.member(tenant,person,grants=(RoleGrant('knowledge_manager','knowledge_collection',str(a)),));token,csrf=await self.session(tenant,person)
  self.people.append(dict(tenant=tenant,person=person,member=member,token=token,csrf=csrf));index=len(self.people)-1
  scoped=await self.org_get('knowledge/collections',index)
  require(scoped.status_code==200 and scoped.json()['tenant_level_allowed'] is False and [item['collection_id'] for item in scoped.json()['items']]==[str(a)],'collections-discovery')
  require((await self.org_get('knowledge/directory',index)).status_code==403,'collection-no-tenant-directory')
  require((await self.org_get('knowledge/directory?collection='+str(a),index)).status_code==200,'exact-collection-picker')
  require((await self.org_get('knowledge/directory?collection='+str(b),index)).status_code==404,'other-collection-hidden')
  self.passed('collection_discovery_and_picker_require_exact_current_manager_scope')
  identity=IdentityService(SimpleNamespace(public_origin=ORIGIN),PostgresIdentityRepository(self.database),None,None)
  app=create_app(Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True}),services=ApplicationServices(health=SimpleNamespace(),identity=identity,organization=self.organization))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
   for who,paths in ((3,('organization/members','organization/members/'+str(mid),'organization/facets?field=group')),(0,('knowledge/directory','knowledge/directory/facets?field=department','knowledge/collections'))):
    headers={**self.headers(who),'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'}
    boot=await client.get('/api/v1/me',headers=headers);require(boot.status_code==200,'boot')
    self.people[who]['csrf']=boot.json()['csrf_token'];binding=boot.json()['context_binding'];headers['x-eai-expected-context']=binding
    for path in paths:
     response=await client.get('/api/v1/'+path,headers=headers)
     require(response.status_code==200 and response.headers.get('x-eai-context-binding')==binding,'g13-org-'+str(response.status_code))
    headers['x-eai-expected-context']='0'*64
    require((await client.get('/api/v1/'+paths[0],headers=headers)).status_code==409,'g13-org-mismatch')
  self.passed('all_six_private_directory_member_facet_collection_reads_are_attested_after_real_restricted_commit')
  for table in ('organization_profiles','organization_requests','private_organization_members'):
   async with self.database.transaction() as connection:
    try:await connection.execute(sql.SQL('SELECT * FROM {}.{}').format(sql.Identifier(self.schema),sql.Identifier(table)))
    except Exception:pass
    else:raise CheckFailure('private-organization-readable')
  await self.execute('GRANT SELECT ON {s}.organization_profiles TO {a}')
  try:require((await self.org_get('organization/members')).status_code==503,'organization-privilege-drift')
  finally:await self.execute('REVOKE SELECT ON {s}.organization_profiles FROM {a}')
  self.passed('profiles_requests_and_view_are_private_and_privilege_drift_disables_capability')
  # Measure encoded payload/response envelopes at maximum configured field sizes.
  groups=[str(n)+'😀'*98 for n in range(32)];maximal={**self.material(),'display_name':'😀'*200,'department':'😀'*100,'job_title':'😀'*100,'groups':groups}
  for _ in range(50):
   member=await self.member(tenant,uuid4(),grants=())
   response=await self.patch(member,{**maximal,'key':str(uuid4())});require(response.status_code==200,'max-profile')
  page=await self.org_get('organization/members?'+urlencode(dict(q='😀',limit=50)))
  require(page.status_code==200 and len(page.json()['items'])==50 and len(page.content)<1_048_576,'max-page-envelope')
  self.capacity=dict(scope='bounded_UTF8_transport_fixture_not_enterprise_load_benchmark',members=50,page_bytes=len(page.content),request_bytes=len(json.dumps(maximal,ensure_ascii=False).encode('utf-8')),groups_per_member=32)
  self.passed('fifty_maximum_utf8_profiles_fit_measured_one_mib_response_and_sixteen_kib_input_envelopes')
  before=await self.scalar('SELECT count(*) FROM {s}.organization_requests')
  require((await self.patch(mid,{**material,'key':str(uuid4()),'groups':['x']*33})).status_code in (400,422),'excess-metadata-rejected')
  require(await self.scalar('SELECT count(*) FROM {s}.organization_requests')==before,'excess-metadata-no-ledger')
  self.passed('oversized_metadata_is_rejected_atomically_without_silent_truncation')


def main():
 logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/bd-fe-01/database-proof.json'));args=parser.parse_args()
 if not args.live:parser.error('Explicit --live required.')
 try:
  settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=19,operation='organization_cases'))
  args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result);print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
 except Exception as error:print('Organization verification failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None

if __name__=='__main__':main()
