"""BD-FE-03 actual restricted PG/API audience proof, controlled ports, no paid calls."""
import argparse
import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from pathlib import Path
from uuid import UUID,uuid4
from psycopg.types.json import Jsonb
from psycopg import sql
from check_knowledge_workspace import verify,Harness as WorkspaceHarness
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.knowledge_check import write_json
from enterprise_ai_platform.modules.knowledge.domain import KnowledgeFailure
from enterprise_ai_platform.rag_check import Harness as RagHarness
from enterprise_ai_platform.conversation_check import ControlledSources
from enterprise_ai_platform.infrastructure.database.conversations import PostgresConversationRepository
from enterprise_ai_platform.modules.conversations.service import ConversationService
from enterprise_ai_platform.modules.identity.service import digest
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.integrations.checkpoints.rag_graph import build_rag_graph
import httpx
from enterprise_ai_platform.composition import ApplicationServices
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.identity.service import IdentityService
from enterprise_ai_platform.infrastructure.database.identity import PostgresIdentityRepository


class Harness(WorkspaceHarness,RagHarness):
 async def before_upgrade(self):
  self.sources=ControlledSources();self.conv_repo=PostgresConversationRepository(self.database,live_sources=True)
  self.conversations=ConversationService(self.conv_repo,ORIGIN,sources=self.sources)
  fid=await self.ready_file('Fictional migration fixture.')
  # Deliberately use the pre0020 wire, before new optional audience fields exist.
  self.legacy_row=await self.knowledge.create(self.people[0]['token'],self.headers(0,'application/json'),dict(file_id=fid,key=str(uuid4()),title='Existing before audience migration',readers=[]))
  await self.process_knowledge();require((await self.mutate(self.legacy_row,'publish')).status_code==200,'pre-migration-publish')
  self.legacy_acl=await self.scalar('SELECT count(*) FROM {s}.document_acl WHERE document_id=%s',(UUID(self.legacy_row['document_id']),))
 async def migration_only(self):
  actual=await self.audience(self.legacy_row)
  require(actual.status_code==200 and actual.json()['audience_mode']=='specific_people' and actual.json()['explicit_readers_count']==self.legacy_acl==1,'existing-migrated-stays-restricted')
  require((await self.source_http(self.legacy_row)).status_code==404,'migration-no-employee-expansion')
  self.passed('existing_pre0020_published_document_migrates_as_specific_people_without_expanding_its_creator_acl')
 async def after_upgrade(self,database):
  self.database=database
  for adapter in (self.repo,self.file_repo,self.conv_repo,self.jobs.repository,self.scheduler.repository):adapter.database=database
 async def boundaries_only(self):
  for table in ('knowledge_selections','knowledge_selection_requests'):
   async with self.database.transaction() as connection:
    try:await connection.execute(sql.SQL('SELECT * FROM {}.{}').format(sql.Identifier(self.schema),sql.Identifier(table)))
    except Exception as error:require(getattr(error,'sqlstate',None)=='42501','snapshot-private-table-denied')
    else:raise CheckFailure('snapshot-table-exposed')
   flags=await self.scalar('SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid=%s::regclass',(self.schema+'.'+table,))
   require(flags is True,'snapshot-forced-rls')
  self.passed('snapshot_and_creation_key_tables_have_forced_rls_and_reject_raw_restricted_reads')
  async with self.database.transaction() as connection:
   for signature in ('_knowledge_access(text,text,jsonb)','_knowledge_selection_live(uuid,uuid,uuid,uuid,uuid,bigint)','_knowledge_selection_receipt('+self.schema+'.knowledge_selections)'):
    permitted=(await (await connection.execute("SELECT has_function_privilege(current_user,%s,'EXECUTE')",(self.schema+'.'+signature,))).fetchone())[0]
    require(permitted is False,'private-snapshot-helper-execute')
  self.passed('all_snapshot_access_and_eligibility_helpers_are_not_executable_by_runtime_login')
  await self.execute('GRANT SELECT ON {s}.knowledge_selections TO {a}')
  try:
   try:await self.knowledge.call(self.people[0]['token'],'audience',dict(id=str(uuid4()),after=None,limit=1))
   except KnowledgeFailure as error:require(error.status==503,'snapshot-privilege-drift-closed')
   else:raise CheckFailure('snapshot-privilege-drift-accepted')
  finally:await self.execute('REVOKE SELECT ON {s}.knowledge_selections FROM {a}')
  self.passed('unexpected_snapshot_read_privilege_disables_the_runtime_capability')
 async def audience(self,row,index=0):
  async with await self.client() as client:return await client.get('/api/v1/knowledge/documents/'+row['document_id']+'/audience',headers=self.headers(index))
 async def select(self,row=None,*,include=(),exclude=(),all_matching=False,filters=None,key=None,index=0,expected=None,collection=None,retain_existing=True):
  version=(await self.get_workspace('/'+row['document_id'],index)).json()['document_version'] if row else None
  material=dict(key=key or str(uuid4()),collection_id=collection,base_document_id=row['document_id'] if row else None,expected_version=expected if expected is not None else version,
   all_matching=all_matching,retain_existing=retain_existing,filters=filters or {},include_ids=list(include),exclude_ids=list(exclude))
  async with await self.client() as client:return await client.post('/api/v1/knowledge/selections',json=material,headers=self.headers(index,'application/json')),material
 async def apply(self,row,mode='all_current_employees',selection=None,*,key=None,index=0,expected=None):
  version=(await self.get_workspace('/'+row['document_id'],index)).json()['document_version']
  material=dict(audience_mode=mode,selection_id=selection,expected_version=expected if expected is not None else version,key=key or str(uuid4()))
  async with await self.client() as client:return await client.patch('/api/v1/knowledge/documents/'+row['document_id']+'/audience',json=material,headers=self.headers(index,'application/json')),material
 async def replay_apply(self,row,material,index=0):
  async with await self.client() as client:return await client.patch('/api/v1/knowledge/documents/'+row['document_id']+'/audience',json=material,headers=self.headers(index,'application/json'))
 async def expire(self):
  await self.execute("WITH e AS(SELECT clock_timestamp()-interval '1 second' expires) UPDATE {s}.knowledge_selections SET expires_at=e.expires,created_at=e.expires-interval '900 seconds' FROM e")
  await self.execute("UPDATE {s}.knowledge_selection_requests SET expires_at=clock_timestamp()-interval '1 second'")
 async def audience_cases(self):
  self.sources=ControlledSources();self.conv_repo=PostgresConversationRepository(self.database,live_sources=True)
  self.conversations=ConversationService(self.conv_repo,ORIGIN,sources=self.sources)
  actual_transaction=self.database.transaction
  @asynccontextmanager
  async def observed_transaction():
   async with actual_transaction() as connection:
    class Observed:
     async def execute(self,query,params=None):
      try:return await connection.execute(query,params)
      except Exception as error:
       code=getattr(error,'sqlstate',None)
       frames=re.findall(r'function ([a-zA-Z0-9_.]+)\([^\n]*?line ([0-9]+)',getattr(getattr(error,'diag',None),'context','') or '')
       print(json.dumps(dict(diagnostic_sqlstate=code if isinstance(code,str) and code.isalnum() and len(code)==5 else 'unknown',frames=frames)),flush=True)
       raise
    yield Observed()
  self.repo.database=SimpleNamespace(schema=self.database.schema,transaction=observed_transaction)
  fid=await self.ready_file('Fictional travel claims require receipts.')
  response=await self.create_document(fid,readers=[]);require(response.status_code==202,'restricted-create');row=response.json()
  await self.process_knowledge();require((await self.mutate(row,'publish')).status_code==200,'publish')
  before=await self.audience(row);require(before.status_code==200,'audience-'+str(before.status_code))
  require(before.json()['audience_mode']=='specific_people' and before.json()['explicit_readers_count']==1,'restricted-default')
  require((await self.source_http(row)).status_code==404,'reader-not-granted')
  for index in (2,3,4):require((await self.audience(row,index)).status_code==404,'foreign-admin-reader-no-acl-metadata')
  self.passed('legacy_admission_remains_restricted_and_current_audience_is_manager_only')
  all_receipt,_=await self.apply(row);require(all_receipt.status_code==200,'all-mode-'+str(all_receipt.status_code))
  require((await self.audience(row)).json()['explicit_readers_count']==0,'all-clears-explicit')
  require((await self.source_http(row)).status_code==200,'all-employee-source')
  require((await self.source_http(row,3)).status_code==404,'admin-is-not-employee')
  self.passed('dynamic_all_employees_grants_published_source_and_clears_creator_acl_without_admin_role_backdoor')
  token,selection_material=await self.select(row,include=[str(self.people[4]['person'])]);require(token.status_code==201,'selection-'+str(token.status_code))
  selection=token.json();require(selection['selected_count']==1,'false-all-matching-does-not-add-tenant')
  async with await self.client() as client:
   replay=await client.post('/api/v1/knowledge/selections',json=selection_material,headers=self.headers(0,'application/json'))
   require(replay.status_code==201 and replay.json()==selection,'selection-key-replay')
   wrong=await client.get('/api/v1/knowledge/selections/'+selection['selection_id'],headers=self.headers(1))
   require(wrong.status_code==404,'selection-owned-not-tenant-shared')
  specific,material=await self.apply(row,'specific_people',selection['selection_id']);require(specific.status_code==200,'specific-'+str(specific.status_code))
  require((await self.source_http(row)).status_code==200 and (await self.audience(row)).json()['explicit_readers_count']==1,'specific-exact')
  require((await self.apply(row))[0].status_code==200,'later-all-mode')
  require((await self.replay_apply(row,material)).json()==specific.json(),'apply-old-receipt')
  require((await self.audience(row)).json()['audience_mode']=='all_current_employees','old-replay-no-overwrite')
  self.passed('frozen_individual_selection_owner_key_and_apply_receipt_preserve_newer_audience')
  await self.expire()
  # A fresh request runs bounded TTL cleanup; business receipt replay is independent.
  fresh,_=await self.select();require(fresh.status_code==201,'cleanup-selector')
  require(await self.scalar('SELECT count(*) FROM {s}.knowledge_selections WHERE id=%s',(UUID(selection['selection_id']),))==0,'snapshot-cleaned')
  require((await self.replay_apply(row,material)).json()==specific.json(),'cleaned-snapshot-business-replay')
  async with await self.client() as client:
   expired=await client.post('/api/v1/knowledge/selections',json=selection_material,headers=self.headers(0,'application/json'))
   require(expired.status_code==409,'expired-create-never-reruns')
  self.passed('expired_snapshot_cleanup_preserves_committed_apply_receipt_and_selection_create_cannot_rerun_old_key')
  admission,admission_material=await self.select(include=[str(self.people[4]['person'])],exclude=[str(self.people[0]['person'])])
  require(admission.status_code==201 and admission.json()['selected_count']==2 and admission.json()['creator_grant_included'] is True,'visible-admission-creator')
  fid=await self.ready_file('Fictional named admission policy.')
  create_material=dict(file_id=fid,key=str(uuid4()),title='Named admission',readers=[],audience_mode='specific_people',selection_id=admission.json()['selection_id'])
  async with await self.client() as client:
   created=await client.post('/api/v1/knowledge/documents',json=create_material,headers=self.headers(0,'application/json'))
  require(created.status_code==202 and created.json()['explicit_readers_count']==2,'selection-admission-'+str(created.status_code))
  admitted=created.json();await self.process_knowledge()
  require((await self.status_document(admitted)).json()['index_state']=='ready','initial-audience-did-not-invalidate-ingestion')
  await self.expire();require((await self.select())[0].status_code==201,'cleanup-admission-selector')
  async with await self.client() as client:
   replay=await client.post('/api/v1/knowledge/documents',json=create_material,headers=self.headers(0,'application/json'))
   require(replay.status_code==202 and replay.json()==admitted,'cleaned-admission-exact-receipt')
   changed=await client.post('/api/v1/knowledge/documents',json={**create_material,'audience_mode':'all_current_employees','selection_id':None},headers=self.headers(0,'application/json'))
   require(changed.status_code==409,'admission-audience-material-binding')
  self.passed('named_admission_creator_snapshot_commits_before_worker_and_exact_receipt_survives_cleanup')
  fid=await self.ready_file('Fictional dynamic admission policy.')
  async with await self.client() as client:
   created=await client.post('/api/v1/knowledge/documents',json=dict(file_id=fid,key=str(uuid4()),title='Dynamic admission',readers=[],audience_mode='all_current_employees'),headers=self.headers(0,'application/json'))
  require(created.status_code==202 and created.json()['explicit_readers_count']==0,'dynamic-admission')
  await self.process_knowledge();require((await self.status_document(created.json())).json()['index_state']=='ready','dynamic-ingestion-ready')
  self.passed('all_employee_admission_has_no_explicit_creator_grant_and_preserves_ingestion')
  await self.derived_consumers(row)
  await self.selection_drift(row)
  await self.large_selection(row)
  await self.legacy_and_locks(row)
  await self.bound_reads_and_terminal(row)
  await self.quotas_and_cleanup()

 async def derived_consumers(self,row):
  # Existing named grants survive loss of the employee role; dynamic mode does not.
  reader=self.people[4]
  await self.execute("DELETE FROM {s}.membership_roles WHERE tenant_id=%s AND membership_id=%s AND role='employee'",(reader['tenant'],reader['member']))
  reader['token'],reader['csrf']=await self.session(reader['tenant'],reader['person'])
  require((await self.source_http(row)).status_code==404,'role-loss-dynamic-revoke')
  token,_=await self.select(row,include=[str(reader['person'])]);require(token.status_code==201,'roleless-named-candidate')
  require((await self.apply(row,'specific_people',token.json()['selection_id']))[0].status_code==200,'roleless-explicit-apply')
  require((await self.source_http(row)).status_code==200,'explicit-survives-employee-loss')
  await self.execute("INSERT INTO {s}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) VALUES(%s,%s,%s,'employee','tenant',%s,%s)",(reader['tenant'],uuid4(),reader['member'],str(reader['tenant']),self.people[3]['person']))
  reader['token'],reader['csrf']=await self.session(reader['tenant'],reader['person'])
  require((await self.apply(row))[0].status_code==200,'restore-all')
  self.passed('dynamic_employee_role_loss_revokes_source_but_active_explicit_named_grant_remains_valid')
  # Real graph/checkpoints with controlled adapters; browser history uses the
  # production database source adapter rather than a permissive fixture guard.
  worker,cid,models=await self.turn('What are the travel claims requirements?')
  result=await worker.run(build_rag_graph)
  history=await self.history(cid,index=4);require(history.status_code==200,'real-history')
  visible=[m for m in history.json()['messages'] if m['author_kind']=='model']
  require(visible and visible[-1]['text'] and not visible[-1]['redacted'],'actual-all-mode-rag-answer')
  require(await self.scalar('SELECT count(*) FROM {s}.checkpoints WHERE run_id=%s',(UUID(worker.repository.binding['run_id']),))>0,'actual-checkpoints')
  fid=await self.ready_file('Fictional travel claims replacement requirements.')
  expected=(await self.get_workspace('/'+row['document_id'])).json()['document_version']
  async with await self.client() as client:
   new=await client.post('/api/v1/knowledge/documents/'+row['document_id']+'/versions',json=dict(file_id=fid,key=str(uuid4()),expected_version=expected),headers=self.headers(0,'application/json'))
  require(new.status_code==202,'replacement-admit');await self.process_knowledge()
  require((await self.mutate(new.json(),'publish')).status_code==200,'replacement-publish')
  old_source=await self.source_http(row)
  require(old_source.status_code==200 and old_source.json()['publication_state']=='superseded','dynamic-audience-applies-to-old-readable-version')
  require(not [m for m in (await self.history(cid,index=4)).json()['messages'] if m['author_kind']=='model'][-1]['redacted'],'superseded-history-remains-visible')
  token,_=await self.select(row);require(token.status_code==201 and token.json()['selected_count']==0,'empty-explicit-snapshot')
  require((await self.apply(row,'specific_people',token.json()['selection_id']))[0].status_code==200,'remove-all-read-access')
  redacted=(await self.history(cid,index=4)).json()['messages']
  model=[m for m in redacted if m['author_kind']=='model'][-1]
  require(model['redacted'] and model['text'] is None and model['citations']==[] and model['sources']==[] and model['feedback'] is None,'actual-history-whole-redaction')
  require((await self.search_http('travel',index=4)).json()['items']==[] and (await self.source_http(row)).status_code==404,'search-source-revoked')
  # File proxy remains a separate owner/manager boundary, not document audience.
  file_id=await self.scalar('SELECT file_id FROM {s}.document_versions WHERE id=%s',(UUID(row['version_id']),))
  require((await self.get(str(file_id),kind='content',index=4)).status_code==404,'file-proxy-not-widened')
  self.passed('actual_controlled_rag_graph_checkpoint_and_database_history_redaction_follow_document_audience_without_widening_file_proxy')
  # A running execution must reject both RAG recheck and checkpoint persistence
  # after its source access disappears, before any model request is performed.
  require((await self.apply(row))[0].status_code==200,'all-before-running-guards')
  active,cid,unused=await self.turn()
  found=await active.repository.call('search',dict(query='travel claims',vector=[1.0]+[0.0]*1023,mode='vector'))
  current_version=(await self.get_workspace('/'+row['document_id'])).json()['active_version_id']
  hits=found['items'];require(hits and current_version in {item['version_id'] for item in hits},'actual-retrieval-visible')
  token,_=await self.select(row);require((await self.apply(row,'specific_people',token.json()['selection_id']))[0].status_code==200,'revoke-running-source')
  try:await active.repository.call('recheck',dict(items=[{key:hit[key] for key in ('version_id','chunk_no','index_generation','access_revision','text_digest')} for hit in hits]))
  except Exception as error:require(getattr(error,'status',None)==403,'actual-rag-recheck-denied')
  else:raise CheckFailure('rag-recheck-leaked')
  refs=[dict(system='platform',kind='document_version',resource_id=hit['version_id'],version=str(hit['version_no']),relation='read',observed_revision=str(hit['access_revision']),page=hit['page'],chunk=hit['chunk_no']) for hit in hits]
  try:await active.saver.repository.call('stage',{**active.saver.binding,'sources':refs,'namespace':'','checkpoint_id':None})
  except Exception as error:require(getattr(error,'status',None)==403,'actual-checkpoint-source-denied')
  else:raise CheckFailure('checkpoint-guard-leaked')
  await active.saver.finish('failed')
  self.passed('running_rag_recheck_and_checkpoint_source_guard_reject_audience_revocation_before_model_egress')

 async def selection_drift(self,row):
  await self.expire()
  reader=self.people[4];tenant=reader['tenant'];creator=self.people[0]['person']
  await self.execute("INSERT INTO {s}.organization_profiles(tenant_id,membership_id,display_name,department,job_title) VALUES(%s,%s,'Budi','Finance','Manager') ON CONFLICT(tenant_id,membership_id) DO UPDATE SET department='Finance',job_title='Manager',version={s}.organization_profiles.version+1",(tenant,reader['member']))
  token,_=await self.select(row,all_matching=True,filters=dict(department='Finance',job_title='Manager'),exclude=[str(creator)])
  require(token.status_code==201 and token.json()['selected_count']==1,'all-filter-snapshot')
  sid=token.json()['selection_id']
  await self.execute("UPDATE {s}.organization_profiles SET department='HR',display_name='Budi changed',version=version+1 WHERE membership_id=%s",(reader['member'],))
  newcomer=uuid4();mid=await self.member(tenant,newcomer,grants=())
  await self.execute("INSERT INTO {s}.organization_profiles(tenant_id,membership_id,display_name,department,job_title) VALUES(%s,%s,'New matching individual','Finance','Manager')",(tenant,mid))
  async with await self.client() as client:
   reviewed=await client.get('/api/v1/knowledge/selections/'+sid,headers=self.headers())
  require(reviewed.status_code==200 and [v['principal_id'] for v in reviewed.json()['items']]==[str(reader['person'])] and reviewed.json()['items'][0]['display_name']=='Budi','frozen-review-not-filter-rerun')
  require((await self.apply(row,'specific_people',sid))[0].status_code==200,'filter-drift-keeps-ids')
  require((await self.audience(row)).json()['readers'][0]['principal_id']==str(reader['person']),'frozen-applied-exact-id')
  self.passed('organization_filter_drift_and_new_matches_do_not_change_frozen_individual_ids_or_review_names')
  token,_=await self.select(row);require(token.status_code==201,'eligibility-token')
  await self.execute('UPDATE {s}.memberships SET active=false WHERE id=%s',(reader['member'],))
  before=(await self.audience(row)).json()
  require((await self.apply(row,'specific_people',token.json()['selection_id']))[0].status_code==409,'eligibility-drift-conflict')
  require((await self.audience(row)).json()==before,'eligibility-conflict-no-overwrite')
  retained,_=await self.select(row);require(retained.status_code==201 and retained.json()['selected_count']==1,'retained-inactive-explicit')
  require((await self.apply(row,'specific_people',retained.json()['selection_id']))[0].status_code==200,'retain-known-inactive-grant')
  require((await self.audience(row)).json()['readers'][0]['eligible'] is False,'inactive-visible-current-acl')
  await self.execute('UPDATE {s}.memberships SET active=true WHERE id=%s',(reader['member'],))
  reader['token'],reader['csrf']=await self.session(tenant,reader['person'])
  self.passed('eligibility_drift_conflicts_atomically_while_existing_inactive_grants_remain_visible_and_explicitly_retainable')

 async def large_selection(self,row):
  await self.expire()
  tenant=self.people[0]['tenant']
  inserted=await self.execute("WITH people AS(INSERT INTO {s}.principals(id,issuer,subject,display_name) SELECT gen_random_uuid(),'urn:eai:audience-fixture',gen_random_uuid()::text,%s FROM generate_series(1,1001) RETURNING id) INSERT INTO {s}.memberships(tenant_id,id,principal_id) SELECT %s,gen_random_uuid(),id FROM people RETURNING principal_id",('😀'*200,tenant))
  ids=[str(value[0]) for value in await inserted.fetchall()]
  existing=(await self.audience(row)).json()['readers'];exclude=[item['principal_id'] for item in existing]
  selected,material=await self.select(row,include=ids[:1000],exclude=exclude)
  require(selected.status_code==201 and selected.json()['selected_count']==1000,'thousand-selection-'+str(selected.status_code))
  sid=selected.json()['selection_id'];seen=[];after=None;largest_page=0
  async with await self.client() as client:
   while True:
    path='/api/v1/knowledge/selections/'+sid+'?limit=50'+('&after='+after if after else '')
    response=await client.get(path,headers=self.headers());require(response.status_code==200,'snapshot-page')
    data=response.json();largest_page=max(largest_page,len(response.content));seen.extend(item['principal_id'] for item in data['items']);after=data['next_after']
    if not after:break
  require(len(seen)==1000 and set(seen)==set(ids[:1000]) and largest_page<1048576,'all-pages-exact')
  applied,_=await self.apply(row,'specific_people',sid);require(applied.status_code==200 and applied.json()['explicit_readers_count']==1000,'thousand-atomic-apply')
  count=await self.scalar('SELECT count(*) FROM {s}.document_acl WHERE document_id=%s',(UUID(row['document_id']),));require(count==1000,'thousand-whole-acl')
  replacement,_=await self.select(row,include=ids[1:1001],retain_existing=False)
  require(replacement.status_code==201 and replacement.json()['selected_count']==1000,'thousand-clear-replacement-selection')
  require((await self.apply(row,'specific_people',replacement.json()['selection_id']))[0].status_code==200,'thousand-clear-replacement-apply')
  actual=await (await self.execute('SELECT principal_id FROM {s}.document_acl WHERE document_id=%s',(UUID(row['document_id']),))).fetchall()
  require({str(value[0]) for value in actual}==set(ids[1:1001]),'clear-replacement-does-not-retain-deselected-id')
  before=(await self.audience(row)).json();overflow,_=await self.select(row,all_matching=True,filters=dict(q='😀'))
  require(overflow.status_code==429 and (await self.audience(row)).json()==before,'overflow-no-partial-acl')
  snapshot_bytes=await self.scalar('SELECT octet_length(items::text) FROM {s}.knowledge_selections WHERE id=%s',(UUID(sid),))
  self.capacity=dict(scope='specific_reader_UTF8_transport_and_atomicity_fixture_not_enterprise_load_benchmark',selected=1000,snapshot_bytes=snapshot_bytes,max_review_page_bytes=largest_page,create_request_bytes=len(json.dumps(material).encode('utf-8')))
  self.passed('one_thousand_named_readers_review_across_pages_and_apply_atomically_with_measured_utf8_bounds_overflow_rejects_whole_action')
  before_snapshots=await self.scalar('SELECT count(*) FROM {s}.knowledge_selections');before_requests=await self.scalar('SELECT count(*) FROM {s}.knowledge_selection_requests')
  creator_overflow,_=await self.select(include=ids[:1000])
  require(creator_overflow.status_code==429,'creator-overflow-whole-rejection')
  require(await self.scalar('SELECT count(*) FROM {s}.knowledge_selections')==before_snapshots and await self.scalar('SELECT count(*) FROM {s}.knowledge_selection_requests')==before_requests,'creator-overflow-no-snapshot')
  oversized,_=await self.select(row,include=ids[:1000],exclude=ids[:1000])
  require(oversized.status_code==413,'whole-browser-payload-over-64k')
  require(await self.scalar('SELECT count(*) FROM {s}.knowledge_selections')==before_snapshots and (await self.audience(row)).json()==before,'oversized-payload-no-side-effect')
  self.passed('creator_added_overflow_and_browser_payload_over_64k_reject_without_snapshot_ledger_or_acl_mutation')
  require((await self.apply(row))[0].status_code==200,'large-all-mode')
  await self.execute("INSERT INTO {s}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) SELECT m.tenant_id,gen_random_uuid(),m.id,'employee','tenant',m.tenant_id::text,%s FROM {s}.memberships m JOIN {s}.principals p ON p.id=m.principal_id WHERE m.tenant_id=%s AND p.issuer='urn:eai:audience-fixture'",(self.people[3]['person'],tenant))
  pid=UUID(ids[-1]);mid=await self.scalar('SELECT id FROM {s}.memberships WHERE tenant_id=%s AND principal_id=%s',(tenant,pid));token,csrf=await self.session(tenant,pid)
  self.people.append(dict(tenant=tenant,person=pid,member=mid,token=token,csrf=csrf));self.bulk_person=self.people[-1]
  require((await self.source_http(row,len(self.people)-1)).status_code==200,'more-than-thousand-dynamic')
  require((await self.audience(row)).json()['explicit_readers_count']==0,'all-mode-does-not-enumerate')
  self.passed('all_current_employees_supports_more_than_one_thousand_members_without_explicit_acl_enumeration')

 async def legacy_and_locks(self,row):
  expected=(await self.get_workspace('/'+row['document_id'])).json()['document_version'];key=str(uuid4())
  require((await self.mutate(row,'acl',expected=expected,key=key,readers=[])).status_code==200,'legacy-new-acl')
  require((await self.audience(row)).json()['audience_mode']=='specific_people','legacy-specific-mode')
  require((await self.apply(row))[0].status_code==200,'later-new-all-mode')
  before=(await self.audience(row)).json()
  require((await self.mutate(row,'acl',expected=expected,key=key,readers=[])).status_code==200,'legacy-old-key-replay')
  require((await self.audience(row)).json()==before,'legacy-replay-keeps-new-mode')
  require((await self.mutate(row,'acl',readers=[])).status_code==200,'legacy-new-empty')
  self.passed('new_legacy_acl_write_is_specific_and_old_receipt_replay_does_not_revert_newer_audience')
  await self.expire()
  reader=self.people[4];selection,_=await self.select(row,include=[str(reader['person'])]);require(selection.status_code==201,'lock-selection')
  before=(await self.audience(row)).json()
  async with self.admin.transaction():
   await self.execute('SELECT id FROM {s}.principals WHERE id=%s FOR UPDATE',(reader['person'],))
   async with asyncio.timeout(3):rejected,material=await self.apply(row,'specific_people',selection.json()['selection_id'])
   require(rejected.status_code==409,'nonblocking-reader-contention')
  require((await self.audience(row)).json()==before,'reader-contention-no-overwrite')
  require((await self.replay_apply(row,material)).status_code==200,'identical-retry-after-known-contention')
  self.passed('reader_identity_lock_contention_returns_whole_action_conflict_and_same_intent_can_be_retried_after_release')
  selection,_=await self.select(row);require(selection.status_code==201,'expiry-selection')
  sid=selection.json()['selection_id'];before=(await self.audience(row)).json();event_count=await self.scalar('SELECT count(*) FROM {s}.knowledge_events')
  task=None
  try:
   async with self.admin.transaction():
    await self.execute("WITH e AS(SELECT clock_timestamp()+interval '1500 milliseconds' expires) UPDATE {s}.knowledge_selections SET expires_at=e.expires,created_at=e.expires-interval '900 seconds' FROM e WHERE id=%s",(UUID(sid),))
    task=asyncio.create_task(self.apply(row,'specific_people',sid));waiting=False
    for _ in range(30):
     await self.execute('SELECT pg_stat_clear_snapshot()')
     waiting=await self.scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=%s AND wait_event_type='Lock')",('eai_runtime_'+self.schema.rsplit('_',1)[1],))
     if waiting:break
     await asyncio.sleep(.05)
    require(waiting,'apply-selection-row-lock-wait')
    await asyncio.sleep(1.6)
   expired,_=await task;require(expired.status_code==409,'expiry-after-lock-no-apply')
  finally:
   if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
  require((await self.audience(row)).json()==before and await self.scalar('SELECT count(*) FROM {s}.knowledge_events')==event_count,'expired-no-audience-or-event-mutation')
  self.passed('selection_expiry_during_actual_row_lock_wait_rejects_without_acl_version_or_audit_mutation')
  # Snapshot JSON does not keep a foreign key to every candidate. An identity
  # removed before apply must become a conflict, not a late partial FK failure.
  candidate=self.bulk_person
  removed,_=await self.select(row,include=[str(candidate['person'])],exclude=[str(reader['person'])]);require(removed.status_code==201,'removed-principal-selection')
  await self.execute('DELETE FROM {s}.sessions WHERE principal_id=%s',(candidate['person'],))
  await self.execute('DELETE FROM {s}.membership_roles WHERE membership_id=%s',(candidate['member'],))
  await self.execute('DELETE FROM {s}.memberships WHERE id=%s',(candidate['member'],))
  await self.execute('DELETE FROM {s}.principals WHERE id=%s',(candidate['person'],))
  before=(await self.audience(row)).json()
  require((await self.apply(row,'specific_people',removed.json()['selection_id']))[0].status_code==409 and (await self.audience(row)).json()==before,'removed-identity-conflict')
  self.passed('removed_candidate_identity_is_an_explicit_eligibility_conflict_before_acl_mutation')

 async def bound_reads_and_terminal(self,row):
  await self.expire();selected,_=await self.select(row);require(selected.status_code==201,'bound-selection')
  identity=IdentityService(SimpleNamespace(public_origin=ORIGIN),PostgresIdentityRepository(self.database),None,None)
  app=create_app(Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True}),services=ApplicationServices(health=SimpleNamespace(),identity=identity,knowledge=self.knowledge))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
   headers={**self.headers(),'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'}
   boot=await client.get('/api/v1/me',headers=headers);require(boot.status_code==200,'audience-bootstrap')
   self.people[0]['csrf']=boot.json()['csrf_token'];binding=boot.json()['context_binding'];headers['x-eai-expected-context']=binding
   for path in ('/api/v1/knowledge/documents/'+row['document_id']+'/audience','/api/v1/knowledge/selections/'+selected.json()['selection_id']):
    response=await client.get(path,headers=headers)
    require(response.status_code==200 and response.headers.get('x-eai-context-binding')==binding,'actual-audience-read-bound')
   headers['cookie']=self.headers(1)['cookie']
   require((await client.get('/api/v1/knowledge/documents/'+row['document_id']+'/audience',headers=headers)).status_code==409,'actual-cookie-mismatch')
  self.passed('current_audience_and_snapshot_review_have_committed_g13_binding_and_reject_wrong_actual_cookie_context')
  collection=uuid4();await self.execute("INSERT INTO {s}.authorization_scopes(tenant_id,scope_kind,scope_ref) VALUES(%s,'knowledge_collection',%s)",(self.people[0]['tenant'],collection))
  async with await self.client() as client:
   upload=await client.post('/api/v1/files?purpose=knowledge&collection_id='+str(collection),content=b'Fictional scoped audience policy.',headers=self.headers())
  require(upload.status_code==202,'scoped-audience-file');await self.inspector.process(await self.scheduler.claim())
  admitted=await self.create_document(upload.json()['id']);require(admitted.status_code==202,'scoped-audience-document');scoped=admitted.json()
  token,_=await self.select(scoped,collection=str(collection));require(token.status_code==201,'scoped-selection')
  path='/api/v1/knowledge/selections/'+token.json()['selection_id']
  await self.execute('UPDATE {s}.authorization_scopes SET active=false WHERE scope_ref=%s',(collection,))
  async with await self.client() as client:require((await client.get(path,headers=self.headers())).status_code==404,'inactive-scope-selection-hidden')
  await self.execute('UPDATE {s}.authorization_scopes SET active=true WHERE scope_ref=%s',(collection,))
  require((await self.mutate(scoped,'withdraw')).status_code==200,'withdraw-snapshot-base')
  async with await self.client() as client:require((await client.get(path,headers=self.headers())).status_code==404,'withdrawn-base-selection-hidden')
  self.passed('snapshot_review_rechecks_base_document_withdrawal_and_current_collection_activity')

 async def seed_snapshots(self,tenant,count,exclude=()):
  # Valid bounded metadata fixture spread across four memberships, at most8 each.
  await self.execute("WITH actors AS(SELECT m.*,row_number() OVER(ORDER BY m.id) n FROM {s}.memberships m WHERE m.tenant_id=%s AND NOT m.principal_id=ANY(%s::uuid[]) ORDER BY m.id LIMIT 4), moment AS(SELECT clock_timestamp() t), snapshots AS(SELECT a.tenant_id,a.principal_id,a.id mid,moment.t,jsonb_build_array(jsonb_build_object('principal_id',a.principal_id,'membership_id',a.id,'display_name',NULL,'membership_active',true,'principal_active',true)) items FROM generate_series(1,%s) i JOIN actors a ON a.n=mod(i-1,4)+1 CROSS JOIN moment) INSERT INTO {s}.knowledge_selections(tenant_id,id,actor_id,key,snapshot_digest,items,creator_grant_included,created_at,expires_at) SELECT tenant_id,gen_random_uuid(),principal_id,gen_random_uuid(),encode(sha256(convert_to(items::text,'UTF8')),'hex'),items,true,t,t+interval '900 seconds' FROM snapshots",(tenant,list(exclude),count))
  await self.execute("INSERT INTO {s}.knowledge_selection_requests(tenant_id,actor_id,key,request_hash,response,expires_at) SELECT tenant_id,actor_id,key,snapshot_digest,{s}._knowledge_selection_receipt(s),expires_at FROM {s}.knowledge_selections s WHERE tenant_id=%s AND NOT EXISTS(SELECT 1 FROM {s}.knowledge_selection_requests r WHERE r.tenant_id=s.tenant_id AND r.actor_id=s.actor_id AND r.key=s.key)",(tenant,))

 async def quotas_and_cleanup(self):
  await self.expire()
  require((await self.select(index=0))[0].status_code==201 and (await self.select(index=1))[0].status_code==201,'quota-cleanup-start')
  tenant=self.people[0]['tenant'];await self.seed_snapshots(tenant,29,exclude=(self.people[0]['person'],self.people[1]['person']))
  require(await self.scalar('SELECT count(*) FROM {s}.knowledge_selections WHERE tenant_id=%s',(tenant,))==31,'quota-tenant31')
  async with asyncio.timeout(5):race=await asyncio.gather(self.select(index=0),self.select(index=1))
  require(sorted(response.status_code for response,_ in race)==[201,429],'tenant-quota-atomic')
  require(await self.scalar('SELECT count(*) FROM {s}.knowledge_selections WHERE tenant_id=%s',(tenant,))==32,'tenant-quota-no-overflow')
  self.passed('different_actors_racing_at_tenant_snapshot_quota_admit_only_one_without_overflow')
  await self.expire();await self.execute('DELETE FROM {s}.knowledge_selections WHERE expires_at<=clock_timestamp()')
  foreign=self.people[2]['tenant']
  for _ in range(4):await self.member(foreign,uuid4(),grants=())
  await self.seed_snapshots(tenant,31,exclude=(self.people[0]['person'],self.people[1]['person']))
  await self.seed_snapshots(foreign,31,exclude=(self.people[2]['person'],))
  for count in (31,31,3):
   other=uuid4()
   for _ in range(4):await self.member(other,uuid4(),grants=())
   await self.seed_snapshots(other,count)
  require(await self.scalar('SELECT count(*) FROM {s}.knowledge_selections')==127,'quota-global127')
  async with asyncio.timeout(5):race=await asyncio.gather(self.select(index=0),self.select(index=2))
  require(sorted(response.status_code for response,_ in race)==[201,429],'global-quota-atomic')
  require(await self.scalar('SELECT count(*) FROM {s}.knowledge_selections')==128,'global-quota-no-overflow')
  self.passed('different_tenants_racing_at_global_snapshot_quota_admit_only_one_without_overflow')
  await self.expire();fresh,_=await self.select(index=0);require(fresh.status_code==201,'bounded-global-expiry-cleanup')
  sid=fresh.json()['selection_id'];await self.expire()
  async with self.admin.transaction():
   await self.execute('SELECT id FROM {s}.knowledge_selections WHERE id=%s FOR SHARE',(UUID(sid),))
   async with asyncio.timeout(3):new,_=await self.select(index=1)
   require(new.status_code==201,'cleanup-skips-locked-review-row')
   require(await self.scalar('SELECT count(*) FROM {s}.knowledge_selections WHERE id=%s',(UUID(sid),))==1,'locked-snapshot-retained')
  require((await self.select(index=1))[0].status_code==201,'cleanup-after-review-release')
  require(await self.scalar('SELECT count(*) FROM {s}.knowledge_selections WHERE id=%s',(UUID(sid),))==0,'unlocked-expired-cleaned')
  self.passed('bounded_cleanup_skips_a_snapshot_share_lock_without_waiting_and_cleans_it_after_release')
  for _ in range(7):require((await self.select(index=0))[0].status_code==201,'actor-quota-seven')
  same=self.people[0];token,csrf=await self.session(same['tenant'],same['person'])
  self.people.append(dict(**{**same,'token':token,'csrf':csrf}));other_session=len(self.people)-1
  async with asyncio.timeout(5):race=await asyncio.gather(self.select(index=0),self.select(index=other_session))
  require(sorted(response.status_code for response,_ in race)==[201,429],'same-actor-two-sessions-quota')
  require(await self.scalar('SELECT count(*) FROM {s}.knowledge_selections WHERE tenant_id=%s AND actor_id=%s',(same['tenant'],same['person']))==8,'actor-quota-no-overflow')
  self.passed('same_actor_in_two_real_sessions_racing_at_snapshot_quota_admits_only_one')


def main():
 logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path);mode=parser.add_mutually_exclusive_group();mode.add_argument('--migration-only',action='store_true');mode.add_argument('--boundaries-only',action='store_true');args=parser.parse_args()
 if not args.live:parser.error('Explicit --live required.')
 try:
  settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
  operation='migration_only' if args.migration_only else 'boundaries_only' if args.boundaries_only else 'audience_cases'
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=20,operation=operation,initial_version=19 if args.migration_only else None))
  output=args.output or Path('local/bd-fe-03/migration-proof.json' if args.migration_only else 'local/bd-fe-03/boundary-proof.json' if args.boundaries_only else 'local/bd-fe-03/database-proof.json')
  output.parent.mkdir(parents=True,exist_ok=True);write_json(output,result);print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
 except Exception as error:print('Knowledge audience verification failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None

if __name__=='__main__':main()
