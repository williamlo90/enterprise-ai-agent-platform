"""Opt-in actual restricted PostgreSQL bound-write checks; owned fixtures only."""
import argparse
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import httpx
from check_knowledge_workspace import Harness as WorkspaceHarness,verify
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import ApplicationServices,database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.identity import PostgresIdentityRepository
from enterprise_ai_platform.infrastructure.database.organization import PostgresOrganizationRepository
from enterprise_ai_platform.infrastructure.database.conversations import PostgresConversationRepository
from enterprise_ai_platform.modules.identity.service import IdentityService
from enterprise_ai_platform.modules.organization.service import OrganizationService
from enterprise_ai_platform.modules.conversations.service import ConversationService
from enterprise_ai_platform.conversation_check import ControlledSources
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.knowledge_check import write_json


class Harness(WorkspaceHarness):
 async def bound_client(self,enabled=True):
  identity=IdentityService(SimpleNamespace(public_origin=ORIGIN),PostgresIdentityRepository(self.database),None,None)
  services=ApplicationServices(health=SimpleNamespace(),identity=identity,organization=self.organization,files=self.files,knowledge=self.knowledge,conversations=self.conversations)
  app=create_app(Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':enabled}),services=services)
  return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN)

 async def bound_headers(self,client,index=0,mime='application/json'):
  headers={**self.headers(index),'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'}
  boot=await client.get('/api/v1/me',headers=headers);require(boot.status_code==200,'bound-bootstrap-'+str(boot.status_code))
  value=boot.json();self.people[index]['csrf']=value['csrf_token']
  return {**self.headers(index,mime),'x-eai-expected-context':value['context_binding']}

 async def bound(self,client,method,path,body,*,status=200,index=0):
  headers=await self.bound_headers(client,index)
  response=await client.request(method,'/api/v1/'+path,json=body,headers=headers)
  require(response.status_code==status,'bound-'+path.rsplit('/',1)[-1]+'-'+str(response.status_code))
  require(response.headers.get('x-eai-context-binding')==headers['x-eai-expected-context'],'committed-binding')
  require(response.headers.get('cache-control')=='no-store','private-no-store')
  return response.json()

 async def browser_writes(self):
  self.organization=OrganizationService(PostgresOrganizationRepository(self.database),ORIGIN)
  self.conversations=ConversationService(PostgresConversationRepository(self.database),ORIGIN,sources=ControlledSources())
  mid=str(self.people[0]['member']);profile=dict(display_name='Bound member',department='Finance',job_title='Manager',groups=['Staff'],expected_version=1,key=str(uuid4()))
  async with await self.bound_client() as client:
   path='/api/v1/organization/members/'+mid;headers=await self.bound_headers(client,3)
   before=await self.scalar('SELECT count(*) FROM {s}.organization_requests')
   missing={k:v for k,v in headers.items() if k!='x-eai-expected-context'}
   require((await client.patch(path,json=profile,headers=missing)).status_code==400,'missing-write-binding')
   require((await client.patch(path,json=profile,headers={**headers,'x-eai-expected-context':'0'*64})).status_code==409,'wrong-write-binding')
   other={**headers,'cookie':'__Host-eai-session='+self.people[0]['token']}
   require((await client.patch(path,json=profile,headers=other)).status_code==409,'actual-cookie-replacement')
   require(await self.scalar('SELECT count(*) FROM {s}.organization_requests')==before,'rejected-before-mutation')
   self.passed('missing_expectation_wrong_binding_and_actual_cookie_replacement_reject_before_organization_write')
   require((await client.patch(path,json=profile,headers={**headers,'x-csrf-token':'z'*43})).status_code==403,'csrf-still-required')
   saved=await self.bound(client,'PATCH','organization/members/'+mid,profile,index=3)
   replay=await self.bound(client,'PATCH','organization/members/'+mid,profile,index=3)
   require(saved==replay and saved['organization_version']==2,'org-bound-receipt')
   self.passed('organization_bound_commit_replay_retains_csrf_cas_and_actual_transaction_attestation')
   conv=await self.bound(client,'POST','conversations',dict(language='id',key=str(uuid4())),status=201,index=4)
   renamed=await self.bound(client,'PATCH','conversations/'+conv['id']+'/name',dict(name='Bound conversation',expected_version=conv['version'],key=str(uuid4())),index=4)
   require(renamed['label']=='Bound conversation','bound-rename-label')
   self.passed('conversation_create_and_rename_attest_real_restricted_owner_transactions')
   upload_headers=await self.bound_headers(client,mime='text/plain')
   upload_key=str(uuid4());upload_headers['idempotency-key']=upload_key
   first=await client.post('/api/v1/files?purpose=knowledge',content=b'Fictional bound file.',headers=upload_headers)
   require(first.status_code==202 and first.headers.get('x-eai-context-binding')==upload_headers['x-eai-expected-context'],'bound-upload')
   second=await client.post('/api/v1/files?purpose=knowledge',content=b'Fictional bound file.',headers=upload_headers)
   require(second.status_code==202 and second.json()['id']==first.json()['id'] and second.headers.get('x-eai-context-binding')==upload_headers['x-eai-expected-context'],'bound-upload-replay')
   self.passed('binary_upload_and_already_bound_replay_finish_with_fresh_committed_status_binding')
  # Drain the single controlled scan job, with no external storage or inference.
  await super().process()
  async with await self.bound_client() as client:
   created=await self.bound(client,'POST','knowledge/documents',dict(file_id=first.json()['id'],title='Bound policy',audience_mode='all_current_employees',key=str(uuid4())),status=202)
  await self.process_knowledge()
  async with await self.bound_client() as client:
   current=await self.current(created);did=created['document_id'];vid=created['version_id']
   published=await self.bound(client,'POST','knowledge/versions/'+vid+'/publish',dict(expected_version=current['document_version'],key=str(uuid4())))
   selected=await self.bound(client,'POST','knowledge/selections',dict(base_document_id=did,expected_version=published['document_version'],retain_existing=False,include_ids=[str(self.people[4]['person'])],key=str(uuid4())),status=201)
   replaced=await self.bound(client,'PATCH','knowledge/documents/'+did+'/audience',dict(audience_mode='specific_people',selection_id=selected['selection_id'],expected_version=published['document_version'],key=str(uuid4())))
   acl=await self.bound(client,'PATCH','knowledge/documents/'+did+'/acl',dict(readers=[str(self.people[4]['person'])],expected_version=replaced['document_version'],key=str(uuid4())))
   self.passed('knowledge_admission_publish_frozen_selection_audience_and_legacy_acl_all_attest_their_exact_final_operations')
  fid=await self.ready_file('Fictional replacement bound file.')
  async with await self.bound_client() as client:
   version=await self.bound(client,'POST','knowledge/documents/'+did+'/versions',dict(file_id=fid,expected_version=acl['document_version'],key=str(uuid4())),status=202)
   withdrawn=await self.bound(client,'POST','knowledge/documents/'+did+'/withdraw',dict(expected_version=version['document_version'],key=str(uuid4())))
   require(withdrawn['withdrawn'] is True,'bound-withdraw')
   self.passed('new_version_and_terminal_withdrawal_attest_committed_context_without_a_model_call')
   headers=await self.bound_headers(client,3)
   require((await client.post('/api/v1/unsupported',json={},headers=headers)).status_code==400,'unsupported-bound-write')
  async with await self.bound_client(False) as client:
   require((await client.patch('/api/v1/organization/members/'+mid,json=profile,headers=headers)).status_code==503,'disabled-bound-write')
  self.passed('disabled_rollout_and_unsupported_mutation_never_silently_ignore_expected_context')


def main():
 logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/browser-writes/database-proof.json'));args=parser.parse_args()
 if not args.live:parser.error('Explicit --live required.')
 try:
  settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
  base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=20,operation='browser_writes'))
  args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result);print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
 except Exception as error:print('Browser write verification failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None

if __name__=='__main__':main()
