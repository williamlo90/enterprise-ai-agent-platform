"""Opt-in BD-FE-02 local API/real restricted DB proof, controlled ingestion only."""
import argparse
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID,uuid4
import httpx
from psycopg import sql
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import ApplicationServices,database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase,direct_connection,parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.identity import PostgresIdentityRepository
from enterprise_ai_platform.infrastructure.database.migration import migrate
from enterprise_ai_platform.infrastructure.database.migration import revisions
from enterprise_ai_platform.infrastructure.database import roles as role_tools
from enterprise_ai_platform.infrastructure.database.roles import provision_role,drop_probe_schema
from enterprise_ai_platform.knowledge_check import source_hashes,write_json,Extractor
from enterprise_ai_platform.knowledge_lifecycle_check import Harness as LifecycleHarness
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.identity.service import IdentityService
from enterprise_ai_platform.modules.authorization.domain import RoleGrant


class Harness(LifecycleHarness):
 async def get_workspace(self,path,index=0):
  async with await self.client() as client:return await client.get('/api/v1/knowledge/documents'+path,headers=self.headers(index))
 async def workspace(self):
  draft=await self.document('Fictional draft private policy.')
  did=draft['document_id'];vid=draft['version_id'];path='/'+did
  catalog=await self.get_workspace('');require(catalog.status_code==200,'catalog-'+str(catalog.status_code))
  item=catalog.json()['items'][0];require(item['document_id']==did and item['active_version'] is None and item['latest_version']['version_id']==vid,'draft-catalog')
  detail=await self.get_workspace(path);require(detail.json()==item,'detail-match')
  versions=await self.get_workspace(path+'/versions');require(versions.status_code==200 and len(versions.json()['items'])==1,'versions')
  self.passed('manager_cross_session_catalog_detail_and_versions_discover_drafts')
  for index in (2,3,4):
   for suffix in (path,path+'/versions',path+'/versions/'+vid+'/preview'):
    require((await self.get_workspace(suffix,index)).status_code==404,'foreign-admin-reader-missing')
  require((await self.get_workspace('',4)).status_code==403,'employee-no-manager-catalog')
  self.passed('foreign_tenant_admin_and_reader_cannot_discover_draft_metadata_or_preview')
  preview=await self.get_workspace(path+'/versions/'+vid+'/preview')
  require(preview.status_code==200,'preview-'+str(preview.status_code));value=preview.json()
  require(value['preview'] is True and value['publication_state']=='draft' and value['chunks'][0]['text']=='Fictional draft private policy.','extracted-preview')
  require((await self.source_http(draft)).status_code==404,'draft-not-reader-source')
  self.passed('ready_exact_draft_preview_is_separate_from_published_reader_source')
  collection=uuid4();tenant=self.people[0]['tenant']
  await self.execute("INSERT INTO {s}.authorization_scopes(tenant_id,scope_kind,scope_ref) VALUES(%s,'knowledge_collection',%s)",(tenant,collection))
  async with await self.client() as client:
   uploaded=await client.post('/api/v1/files?purpose=knowledge&collection_id='+str(collection),content=b'Collectionscope fictional policy.',headers=self.headers())
  require(uploaded.status_code==202,'collection-upload');await self.inspector.process(await self.scheduler.claim())
  collection_row=await self.create_document(uploaded.json()['id'],title='Collectionscope policy');require(collection_row.status_code==202,'collection-document')
  await self.process_knowledge();collection_row=collection_row.json()
  person=uuid4();member=await self.member(tenant,person,grants=(RoleGrant('knowledge_manager','knowledge_collection',str(collection)),));token,csrf=await self.session(tenant,person)
  self.people.append(dict(tenant=tenant,person=person,member=member,token=token,csrf=csrf));manager=len(self.people)-1
  scoped=await self.get_workspace('',manager)
  require(scoped.status_code==200 and collection_row['document_id'] in scoped.text and did not in scoped.text,'scoped-catalog')
  require((await self.get_workspace('?collection=tenant',manager)).status_code==404,'scoped-no-uncollected')
  require((await self.get_workspace(path,manager)).status_code==404,'scoped-no-uncollected-detail')
  self.passed('collection_only_manager_catalog_excludes_uncollected_documents_and_denies_tenant_filter')
  async with self.admin.transaction():
   await self.execute('SELECT id FROM {s}.outbox_jobs WHERE id=%s FOR UPDATE',(UUID(vid),))
   async with asyncio.timeout(3):
    busy=await self.get_workspace(path+'/versions/'+vid+'/preview')
   require(busy.status_code==409,'busy-completed-job-conflict')
  require((await self.get_workspace(path+'/versions/'+vid+'/preview')).status_code==200,'job-contention-recovered')
  self.passed('contested_completed_job_returns_conflict_without_reverse_order_lock_wait')
  pending=await self.document('Pending fictional policy.',ready=False)
  require((await self.get_workspace('/'+pending['document_id']+'/versions/'+pending['version_id']+'/preview')).status_code==409,'pending-not-ready')
  require((await self.get_workspace(path+'/versions/'+pending['version_id']+'/preview')).status_code==404,'wrong-version-pair')
  self.passed('pending_preview_returns_conflict_and_wrong_document_version_is_hidden')
  first=(await self.get_workspace('?limit=1')).json();require(len(first['items'])==1 and first['next_after'] is not None,'page-first')
  second=(await self.get_workspace('?limit=1&after='+first['next_after'])).json()
  require(len(second['items'])==1 and first['items'][0]['document_id']!=second['items'][0]['document_id'],'page-second')
  require((await self.get_workspace('?q=%25')).json()['items']==[],'literal-percent')
  require((await self.get_workspace('?after='+str(uuid4()))).status_code==404,'unknown-cursor')
  require((await self.get_workspace('?collection='+str(uuid4()))).status_code==404,'foreign-collection')
  self.passed('bounded_scoped_keyset_pagination_and_literal_title_search')
  # An actual fresh session for the same owner discovers the same document.
  token,csrf=await self.session(self.people[0]['tenant'],self.people[0]['person'])
  async with await self.client() as client:
   response=await client.get('/api/v1/knowledge/documents',headers={'cookie':'__Host-eai-session='+token})
   require(response.status_code==200 and did in response.text,'fresh-session-discovery')
  self.passed('catalog_persists_across_genuine_application_sessions_without_client_cache')
  identity=IdentityService(SimpleNamespace(public_origin=ORIGIN),PostgresIdentityRepository(self.database),None,None)
  app=create_app(Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True}),services=ApplicationServices(health=SimpleNamespace(),identity=identity,knowledge=self.knowledge))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
   headers={**self.headers(),'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'}
   boot=await client.get('/api/v1/me',headers=headers);require(boot.status_code==200,'boot')
   self.people[0]['csrf']=boot.json()['csrf_token']
   binding=boot.json()['context_binding'];headers['x-eai-expected-context']=binding
   for suffix in ('',path,path+'/versions',path+'/versions/'+vid+'/preview'):
    response=await client.get('/api/v1/knowledge/documents'+suffix,headers=headers)
    require(response.status_code==200 and response.headers.get('x-eai-context-binding')==binding,'bound-workspace')
   headers['x-eai-expected-context']='0'*64
   require((await client.get('/api/v1/knowledge/documents',headers=headers)).status_code==409,'wrong-context')
  self.passed('all_four_private_workspace_reads_use_actual_committed_g13_attestation')
  original_call=self.repo.call
  async def deleted_between(token,operation,payload):
   if operation=='preview_recheck':await self.execute("UPDATE {s}.files SET deletion_state='deletion_pending' WHERE id=%s",(UUID(item['latest_version']['file_id']),))
   return await original_call(token,operation,payload)
  self.repo.call=deleted_between
  try:require((await self.get_workspace(path+'/versions/'+vid+'/preview')).status_code==404,'delete-between-preview')
  finally:self.repo.call=original_call
  self.passed('file_deletion_between_preview_and_recheck_discards_all_extracted_text')
  await self.execute("UPDATE {s}.files SET deletion_state='active' WHERE id=%s",(UUID(item['latest_version']['file_id']),))
  async def changed_between(token,operation,payload):
   if operation=='preview_recheck':await self.execute('UPDATE {s}.documents SET access_revision=access_revision+1 WHERE id=%s',(UUID(did),))
   return await original_call(token,operation,payload)
  self.repo.call=changed_between
  try:require((await self.get_workspace(path+'/versions/'+vid+'/preview')).status_code==409,'stale-preview-binding')
  finally:self.repo.call=original_call
  self.passed('changed_access_revision_between_preview_reads_rejects_exact_binding')
  task=None
  try:
   async with self.admin.transaction():
    await self.execute('UPDATE {s}.authorization_scopes SET active=false WHERE scope_ref=%s',(collection,))
    task=asyncio.create_task(self.get_workspace('?collection='+str(collection)+'&limit=1'))
    waiting=False
    for _ in range(30):
     await self.execute('SELECT pg_stat_clear_snapshot()')
     waiting=await self.scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=%s AND wait_event_type='Lock')",('eai_runtime_'+self.schema.rsplit('_',1)[1],))
     if waiting:break
     await asyncio.sleep(.05)
    require(waiting,'scope-lock-wait-observed')
   raced=await task
   require(raced.status_code==409,'scope-drift-no-truncated-page')
  finally:
   if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
  require((await self.get_workspace('?collection='+str(collection))).status_code==404,'inactive-scope-filter')
  self.passed('collection_deactivation_after_limit_during_lock_wait_rejects_whole_page')
  # Keep pending job ahead of publish irrelevant: publishing is the existing
  # synchronous metadata transaction, with no worker/model invocation here.
  published=await self.mutate(draft,'publish');require(published.status_code==200,'publish-'+str(published.status_code))
  detail=(await self.get_workspace(path)).json();require(detail['active_version']['version_id']==vid,'published-pointer')
  require((await self.get_workspace(path+'/versions/'+vid+'/preview')).json()['publication_state']=='published','published-manager-preview')
  require((await self.mutate(draft,'withdraw')).status_code==200,'withdraw')
  require(did not in (await self.get_workspace('')).text,'withdraw-catalog')
  require((await self.get_workspace(path)).status_code==404 and (await self.get_workspace(path+'/versions/'+vid+'/preview')).status_code==404,'withdraw-detail-preview')
  self.passed('existing_publish_pointer_and_terminal_withdrawal_control_workspace_visibility')


async def verify(direct,base,*,harness_type=Harness,target_version=17,operation='workspace',initial_version=None,direct_timeout=180):
 require(type(direct_timeout) in (int,float) and 15<=direct_timeout<=1200,'bounded-fixture-direct-timeout')
 schema='db03_probe_'+uuid4().hex;extension_schema='eai_vector_probe_'+uuid4().hex;owned_extension=False;owned=False;database=None;stage='preflight'
 before=await migrate(direct);sources=source_hashes()
 try:
  async with direct_connection(direct) as connection:
   if not (await (await connection.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone()):
    async with connection.transaction():
     await connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(extension_schema)))
     await connection.execute(sql.SQL('CREATE EXTENSION vector WITH SCHEMA {}').format(sql.Identifier(extension_schema)))
    owned_extension=True
   require((await (await connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone()) is None,'namespace-unowned')
  stage='migration';await migrate(direct,schema=schema,upgrade=True,target_version=initial_version if initial_version is not None else target_version);owned=True
  role_version=initial_version if initial_version is not None else target_version
  if role_version<len(revisions()):
   # The production provisioner must require its complete manifest. For this
   # explicitly owned legacy fixture only, its complete manifest is the exact
   # historical prefix. No production guard or function file is weakened.
   original_status=role_tools.migrate
   async def fixture_status(role_target,**kwargs):
    require(kwargs.get('schema')==schema,'legacy-role-fixture-namespace')
    return await migrate(role_target,items=revisions()[:role_version],**kwargs)
   role_tools.migrate=fixture_status
   try:target=await provision_role(direct,base,schema=schema,role_name='eai_runtime_'+schema.rsplit('_',1)[1])
   finally:role_tools.migrate=original_status
  else:target=await provision_role(direct,base,schema=schema,role_name='eai_runtime_'+schema.rsplit('_',1)[1])
  database=RuntimeDatabase(target,schema=schema);await database.open()
  async with direct_connection(direct,timeout=direct_timeout) as connection:
   h=harness_type(connection,database,schema,Extractor());stage='fixtures';await h.setup_lifecycle()
   if initial_version is not None:
    stage='before-upgrade';await h.before_upgrade();await database.close()
    stage='feature-upgrade';await migrate(direct,schema=schema,upgrade=True,target_version=target_version)
    database=RuntimeDatabase(target,schema=schema);await database.open();await h.after_upgrade(database)
   stage=operation;await getattr(h,operation)()
  require(await migrate(direct)==before,'platform-unchanged');require(source_hashes()==sources,'source-stable')
  return dict(passed=True,checks=h.checks,check_count=len(h.checks),tested_revision=target_version,platform_revision=before[0],platform_schema_modified=False,
   source_sha256=sources,actual_paid_inference_calls=0,ingestion='controlled_storage_parser_embedding',cleanup='completed',
   **({'capacity':h.capacity} if hasattr(h,'capacity') else {}))
 except Exception as error:raise CheckFailure(stage+':'+(str(error) if isinstance(error,CheckFailure) else type(error).__name__)) from None
 finally:
  if database is not None:await database.close()
  if owned:await drop_probe_schema(direct,schema)
  if owned_extension:
   async with direct_connection(direct) as connection:
    found=await (await connection.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'")).fetchone()
    require(found==(extension_schema,),'owned-extension-cleanup');await connection.execute('DROP EXTENSION vector')
    await connection.execute(sql.SQL('DROP SCHEMA {}').format(sql.Identifier(extension_schema)))

def main():
 logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/bd-fe-02/database-proof.json'));args=parser.parse_args()
 if not args.live:parser.error('Explicit --live required.')
 try:
  settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base))
  args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result);print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
 except Exception as error:print('Knowledge workspace verification failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None

if __name__=='__main__':main()
