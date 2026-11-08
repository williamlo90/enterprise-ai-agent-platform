"""Opt-in BD-FE-04 restricted PostgreSQL/API proof; disposable schema, no inference."""
import argparse
import asyncio
import json
import logging
from pathlib import Path
from uuid import UUID,uuid4

import httpx
from psycopg import sql

from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import ApplicationServices,database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.conversation_check import Harness as ConversationHarness
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase,direct_connection,parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.identity import PostgresIdentityRepository
from enterprise_ai_platform.infrastructure.database.migration import migrate
from enterprise_ai_platform.infrastructure.database.roles import provision_role,drop_probe_schema
from enterprise_ai_platform.knowledge_check import source_hashes,write_json
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.identity.service import IdentityService,digest
from types import SimpleNamespace


class Harness(ConversationHarness):
 async def rename(self,cid,material,index=0,headers=None):
  async with await self.client() as client:
   return await client.patch('/api/v1/conversations/'+cid+'/name',json=material,headers=headers or self.headers(index,'application/json'))
 async def labels(self):
  response=await self.create_conversation();require(response.status_code==201,'create')
  row=response.json();cid=row['id'];require(row['label'] is None and row['label_source']=='empty','empty')
  self.passed('empty_conversation_has_localizable_null_label')
  question='Fictional first question '+('x'*4000)
  sent=await self.append(cid,text=question);require(sent.status_code==202,'append')
  listed=(await self.listing()).json()['items'][0]
  require(listed['label']==question and listed['label_source']=='first_question','full-label')
  history=(await self.history(cid)).json();original_messages=history['messages']
  require(history['conversation']['label']==question,'history-label')
  self.passed('first_human_question_full_over_4000_chars_without_history_fanout')
  material=dict(name='Nama lengkap fiktif\n'+('漢'*1000),expected_version=listed['version'],key=str(uuid4()))
  for index in (1,2,3):require((await self.rename(cid,material,index)).status_code==404,'foreign-owner')
  require((await self.rename(str(uuid4()),material)).status_code==404,'missing')
  bad=self.headers(0,'application/json');bad['origin']='https://foreign.example'
  require((await self.rename(cid,material,headers=bad)).status_code==403,'csrf')
  self.passed('colleague_foreign_tenant_admin_missing_and_bad_csrf_cannot_rename')
  receipt=await self.rename(cid,material);require(receipt.status_code==200,'rename-'+str(receipt.status_code));value=receipt.json()
  require(value['label']==material['name'] and value['version']==listed['version']+1,'receipt')
  require((await self.rename(cid,material)).json()==value,'exact-replay')
  require((await self.rename(cid,{**material,'name':'changed'})).status_code==409,'changed-key')
  require((await self.rename(cid,{**material,'key':str(uuid4())})).status_code==409,'stale-version')
  current=(await self.history(cid)).json()
  require(current['messages']==original_messages and current['conversation']['language']==row['language']
    and current['conversation']['active_run_id']==listed['active_run_id'],'history-unmodified')
  self.passed('rename_atomic_cas_exact_replay_and_conflicts_preserve_messages_language_and_run')
  second={**material,'key':str(uuid4()),'name':'Second fictional name','expected_version':value['version']}
  require((await self.rename(cid,second)).status_code==200,'second')
  require((await self.rename(cid,material)).json()==value,'old-receipt-stable')
  require((await self.listing()).json()['items'][0]['label']==second['name'],'old-replay-no-write')
  self.passed('replay_after_later_rename_returns_original_receipt_without_overwriting_current_name')
  collision=str(uuid4());created=await self.create_conversation(key=collision)
  require(created.status_code==201,'collision-create')
  require((await self.rename(cid,{**second,'key':collision})).status_code==409,'create-key-collision')
  self.passed('create_and_rename_share_key_namespace_without_overwriting_receipts')
  # Two genuine sessions propose different names from the same known version.
  other_token,other_csrf=await self.session(self.people[0]['tenant'],self.people[0]['person'])
  other_headers={**self.headers(0,'application/json'),'cookie':'__Host-eai-session='+other_token,'x-csrf-token':other_csrf}
  current=(await self.listing()).json()['items']
  version=next(item['version'] for item in current if item['id']==cid)
  first={**second,'key':str(uuid4()),'name':'Session one','expected_version':version}
  require((await self.rename(cid,first)).status_code==200,'session-one')
  require((await self.rename(cid,{**first,'key':str(uuid4()),'name':'Session two'},headers=other_headers)).status_code==409,'session-two-stale')
  self.passed('two_real_sessions_using_same_expected_version_only_one_rename_commits')
  second=first
  async with self.database.transaction() as connection:
   # Use a regprocedure text parameter; no identifiers from operator input.
   permitted=(await (await connection.execute('SELECT has_function_privilege(current_user,%s,\'EXECUTE\')',(self.schema+'._conversation_v11_call(text,text,jsonb)',))).fetchone())[0]
   require(permitted is False,'private-helper')
  self.passed('runtime_cannot_execute_private_legacy_capability')
  identity=IdentityService(SimpleNamespace(public_origin=ORIGIN),PostgresIdentityRepository(self.database),None,None)
  app=create_app(Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True}),
    services=ApplicationServices(health=SimpleNamespace(),identity=identity,conversations=self.conversations))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
   headers={'cookie':self.headers()['cookie'],'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'}
   boot=await client.get('/api/v1/me',headers=headers);require(boot.status_code==200,'bootstrap')
   binding=boot.json()['context_binding'];headers['x-eai-expected-context']=binding
   for path in ('/api/v1/conversations','/api/v1/conversations/'+cid+'/messages'):
    response=await client.get(path,headers=headers)
    require(response.status_code==200 and response.headers.get('x-eai-context-binding')==binding and second['name'] in response.text,'bound-label')
   headers['x-eai-expected-context']='0'*64
   require((await client.get('/api/v1/conversations',headers=headers)).status_code==409,'wrong-context')
  self.passed('list_and_history_label_use_committed_g13_binding_and_reject_wrong_context')
  # Expire a separate session while it waits behind an owner row lock. The
  # authorization finish must roll the write back, even though context passed.
  expiring,csrf=await self.session(self.people[0]['tenant'],self.people[0]['person'])
  await self.execute("WITH expiry AS(SELECT clock_timestamp()+interval '1500 milliseconds' t) UPDATE {s}.sessions SET expires_at=expiry.t,authenticated_at=expiry.t-interval '8 hours' FROM expiry WHERE token_hash=%s",(digest('session',expiring),))
  expiry_headers={**self.headers(0,'application/json'),'cookie':'__Host-eai-session='+expiring,'x-csrf-token':csrf}
  version=next(item['version'] for item in (await self.listing()).json()['items'] if item['id']==cid)
  expiry_material=dict(name='Must not commit',expected_version=version,key=str(uuid4()))
  task=None
  try:
   async with self.admin.transaction():
    await self.execute('SELECT id FROM {s}.conversations WHERE id=%s FOR UPDATE',(UUID(cid),))
    task=asyncio.create_task(self.rename(cid,expiry_material,headers=expiry_headers))
    for _ in range(20):
     waiting=await self.scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=%s AND wait_event_type='Lock')",('eai_runtime_'+self.schema.rsplit('_',1)[1],))
     if waiting:break
     await asyncio.sleep(.05)
    require(waiting,'expiry-lock-wait-observed')
    await asyncio.sleep(1.6)
   response=await task
   require(response.status_code in (401,503),'expired-no-success')
  finally:
   if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
  name=await self.scalar('SELECT manual_name FROM {s}.conversations WHERE id=%s',(UUID(cid),))
  require(name==second['name'],'expired-write-rolled-back')
  self.passed('expiry_after_actual_conversation_lock_wait_rolls_back_rename')
  await self.execute("DELETE FROM {s}.membership_roles WHERE membership_id=%s AND role='employee'",(self.people[0]['member'],))
  require((await self.rename(cid,material)).status_code in (401,404),'role-revoked-replay')
  self.passed('current_employee_role_revoke_rejects_original_receipt_replay')
  await self.execute('UPDATE {s}.sessions SET revoked_at=clock_timestamp() WHERE token_hash=%s',(digest('session',self.people[0]['token']),))
  require((await self.rename(cid,material)).status_code==401,'revoked-replay')
  self.passed('revoked_session_cannot_read_receipt_or_replay_rename')


async def verify(direct,base):
 schema='db03_probe_'+uuid4().hex;extension_schema='eai_vector_probe_'+uuid4().hex;owned_extension=False
 owned=False;database=None;stage='preflight';before=await migrate(direct);sources=source_hashes()
 try:
  async with direct_connection(direct) as connection:
   if not (await (await connection.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone()):
    async with connection.transaction():
     await connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(extension_schema)))
     await connection.execute(sql.SQL('CREATE EXTENSION vector WITH SCHEMA {}').format(sql.Identifier(extension_schema)))
    owned_extension=True
   require((await (await connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone()) is None,'namespace-unowned')
  stage='migration';await migrate(direct,schema=schema,upgrade=True,target_version=16);owned=True
  target=await provision_role(direct,base,schema=schema,role_name='eai_runtime_'+schema.rsplit('_',1)[1])
  database=RuntimeDatabase(target,schema=schema);await database.open()
  async with direct_connection(direct,timeout=120) as connection:
   h=Harness(connection,database,schema);stage='fixtures';await h.setup_files();stage='labels';await h.labels()
  require(await migrate(direct)==before,'platform-unchanged');require(source_hashes()==sources,'source-stable')
  return dict(passed=True,checks=h.checks,check_count=len(h.checks),tested_revision=16,platform_revision=before[0],
   platform_schema_modified=False,source_sha256=sources,actual_paid_inference_calls=0,
   oidc='controlled_sessions_not_real_Cognito',cleanup='completed')
 except Exception as error:raise CheckFailure(stage+':'+(str(error) if isinstance(error,CheckFailure) else type(error).__name__)) from None
 finally:
  if database is not None:await database.close()
  if owned:await drop_probe_schema(direct,schema)
  if owned_extension:
   async with direct_connection(direct) as connection:
    found=await (await connection.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'")).fetchone()
    require(found==(extension_schema,),'owned-extension-cleanup')
    await connection.execute('DROP EXTENSION vector')
    await connection.execute(sql.SQL('DROP SCHEMA {}').format(sql.Identifier(extension_schema)))


def main():
 logging.disable(logging.CRITICAL)
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/bd-fe-04/database-proof.json'));args=parser.parse_args()
 if not args.live:parser.error('Explicit --live required.')
 try:
  settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
  base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base))
  args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result)
  print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
 except Exception as error:
  print('Conversation label verification failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None

if __name__=='__main__':main()
