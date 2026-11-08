"""BD-FE-02 period admission plus actual RAG retrieval/guards; no model calls."""
import argparse
import asyncio
from datetime import timedelta
import json
import logging
from pathlib import Path
from uuid import UUID,uuid4
from psycopg import sql
from psycopg.types.json import Jsonb
from check_knowledge_workspace import verify,Harness as WorkspaceHarness
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings,load_settings
from enterprise_ai_platform.conversation_check import ControlledSources
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.conversations import PostgresConversationRepository
from enterprise_ai_platform.knowledge_check import write_json
from enterprise_ai_platform.modules.conversations.service import ConversationService
from enterprise_ai_platform.modules.identity.service import digest
from enterprise_ai_platform.rag_check import Harness as RagHarness


class Harness(RagHarness):
 get_workspace=WorkspaceHarness.get_workspace
 async def admission(self,fid,*,key=None,dates=None):
  material=dict(file_id=fid,key=key or str(uuid4()),title='Fictional period claims policy',readers=[str(self.people[4]['person'])])
  material.update(dates or {})
  async with await self.client() as client:
   return await client.post('/api/v1/knowledge/documents',json=material,headers=self.headers(0,'application/json')),material
 async def counts(self):
  return tuple([await self.scalar('SELECT count(*) FROM {s}.'+table) for table in ('documents','document_versions','document_acl','knowledge_requests','outbox_jobs')])
 async def period(self):
  self.sources=ControlledSources();self.conv_repo=PostgresConversationRepository(self.database)
  self.conversations=ConversationService(self.conv_repo,ORIGIN,sources=self.sources)
  today=await self.scalar("SELECT (clock_timestamp() AT TIME ZONE 'Asia/Singapore')::date")
  cases=[{},dict(valid_from=today.isoformat(),valid_until=(today+timedelta(days=1)).isoformat()),
   dict(valid_from=(today+timedelta(days=1)).isoformat()),dict(valid_until=today.isoformat())]
  rows=[];materials=[]
  for dates in cases:
   fid=await self.ready_file('Fictional travel claims period policy.')
   response,material=await self.admission(fid,dates=dates);require(response.status_code==202,'period-create-'+str(response.status_code))
   row=response.json();rows.append(row);materials.append(material)
   require(row['valid_from']==dates.get('valid_from') and row['valid_until']==dates.get('valid_until'),'admission-period')
   async with await self.client() as client:
    replay=await client.post('/api/v1/knowledge/documents',json={**material,**dict(valid_from=dates.get('valid_from'),valid_until=dates.get('valid_until'))},headers=self.headers(0,'application/json'))
    require(replay.status_code==202 and replay.json()==row,'normalized-replay')
    before=await self.counts()
    changed=await client.post('/api/v1/knowledge/documents',json={**material,'valid_from':(today-timedelta(days=2)).isoformat()},headers=self.headers(0,'application/json'))
    require(changed.status_code==409 and await self.counts()==before,'changed-dates-no-mutation')
   await self.process_knowledge();require((await self.mutate(row,'publish')).status_code==200,'publish-period')
  self.passed('optional_periods_and_legacy_omitted_null_replays_bind_original_intent_without_extra_rows')
  self.passed('changed_dates_same_key_rejected_without_version_job_document_or_ledger_mutation')
  for row,dates in zip(rows,cases):
   did,vid=row['document_id'],row['version_id'];path='/'+did
   status=(await self.status_document(row)).json();detail=(await self.get_workspace(path)).json()
   catalog=next(item for item in (await self.get_workspace('')).json()['items'] if item['document_id']==did)
   version=(await self.get_workspace(path+'/versions')).json()['items'][0]
   preview=(await self.get_workspace(path+'/versions/'+vid+'/preview')).json()
   source=(await self.source_http(row)).json()
   for obj in (status,catalog['latest_version'],catalog['active_version'],detail['latest_version'],detail['active_version'],version,preview,source):
    require(obj['valid_from']==dates.get('valid_from') and obj['valid_until']==dates.get('valid_until'),'all-dto-period')
  self.passed('status_catalog_detail_versions_preview_and_reader_source_share_existing_valid_period_columns')
  refs=[]
  for row in rows:
   detail=(await self.get_workspace('/'+row['document_id'])).json()
   refs.append(dict(system='platform',kind='document_version',resource_id=row['version_id'],version='1',relation='read',observed_revision=str(detail['access_revision']),page=1,chunk=0))
  async with self.database.transaction() as connection:
   for history,expected in ((False,[True,True,False,False]),(True,[True]*4)):
    result=(await (await connection.execute(sql.SQL('SELECT {}.document_sources_call(%s,%s,%s)').format(sql.Identifier(self.schema)),
     (Jsonb(dict(token=digest('session',self.people[4]['token']))),Jsonb(refs),history))).fetchone())[0]
    require(result['status']=='ok' and result['decisions']==expected,'actual-source-period-guard')
  self.passed('current_source_guard_uses_inclusive_start_exclusive_end_singapore_date_while_history_preserves_access')
  worker,cid,models=await self.turn()
  found=await worker.repository.call('search',dict(query='travel claims',vector=[1.0]+[0.0]*1023,mode='vector'))
  found_ids={item['version_id'] for item in found['items']}
  require(found_ids=={rows[0]['version_id'],rows[1]['version_id']},'actual-retrieval-period-filter')
  require(all(not model.calls for model in models),'no-model-calls')
  await worker.saver.finish('failed')
  self.passed('actual_restricted_rag_retrieval_filters_future_and_expired_before_ranking_without_model_calls')
  # New version is independently admitted; changing dates never edits the old
  # published version or its historical period.
  old=rows[1];new_file=await self.ready_file('Fictional replacement travel claims policy.')
  expected=(await self.get_workspace('/'+old['document_id'])).json()['document_version']
  material=dict(file_id=new_file,key=str(uuid4()),expected_version=expected,valid_from=(today-timedelta(days=1)).isoformat(),valid_until=(today+timedelta(days=2)).isoformat())
  async with await self.client() as client:
   path='/api/v1/knowledge/documents/'+old['document_id']+'/versions'
   created=await client.post(path,json=material,headers=self.headers(0,'application/json'));require(created.status_code==202,'period-version')
   before=await self.counts()
   require((await client.post(path,json={**material,'valid_until':(today+timedelta(days=3)).isoformat()},headers=self.headers(0,'application/json'))).status_code==409,'version-period-key-conflict')
   require(await self.counts()==before,'version-period-conflict-no-mutation')
  require((await self.get_workspace('/'+old['document_id'])).json()['active_version_id']==old['version_id'],'new-draft-does-not-replace')
  old_status=(await self.status_document(old)).json();require(old_status['valid_from']==cases[1]['valid_from'] and old_status['valid_until']==cases[1]['valid_until'],'old-period-unchanged')
  await self.process_knowledge()
  require((await self.mutate(created.json(),'publish')).status_code==200,'publish-new-period-version')
  historical=(await self.source_http(old)).json()
  require(historical['publication_state']=='superseded' and historical['valid_from']==cases[1]['valid_from'] and historical['valid_until']==cases[1]['valid_until'],'superseded-source-period')
  async with self.database.transaction() as connection:
   for history,allowed in ((False,False),(True,True)):
    result=(await (await connection.execute(sql.SQL('SELECT {}.document_sources_call(%s,%s,%s)').format(sql.Identifier(self.schema)),
     (Jsonb(dict(token=digest('session',self.people[4]['token']))),Jsonb([refs[1]]),history))).fetchone())[0]
    require(result['status']=='ok' and result['decisions']==[allowed],'superseded-period-history-guard')
  self.passed('new_version_dates_bind_key_and_preserve_published_version_and_history_metadata')
  # Direct restricted capability is also strict, independent from API validation.
  before=await self.counts()
  async with self.database.transaction() as connection:
   base={**materials[0],'id':str(uuid4()),'version_id':str(uuid4()),'key':str(uuid4()),'csrf_hash':digest('csrf',self.people[0]['csrf'])}
   for dates in ({'valid_from':'2026-02-29'},{'valid_from':'2026-11-05','valid_until':'2026-11-05'},{'valid_from':'infinity'}):
    invalid=(await (await connection.execute(sql.SQL('SELECT {}.knowledge_call(%s,%s,%s)').format(sql.Identifier(self.schema)),(digest('session',self.people[0]['token']),'create',Jsonb({**base,**dates})))).fetchone())[0]
    require(invalid['status']=='invalid','database-period-validation')
  require(await self.counts()==before,'invalid-period-no-mutation')
  self.passed('database_capability_rejects_invalid_and_empty_intervals_without_admission_side_effects')
  # Pause only the new metadata write after the legacy capability's finish.
  # Expiry at the outer finish must roll back the entire admission transaction.
  fid=await self.ready_file('Fictional expired-admission fixture.')
  token,csrf=await self.session(self.people[0]['tenant'],self.people[0]['person'])
  await self.execute("CREATE FUNCTION {s}.period_pause() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN PERFORM pg_sleep(1.2);RETURN NEW;END;$$")
  await self.execute('REVOKE ALL ON FUNCTION {s}.period_pause() FROM PUBLIC,{a}')
  await self.execute('CREATE TRIGGER period_pause BEFORE UPDATE OF valid_from,valid_until ON {s}.document_versions FOR EACH ROW EXECUTE FUNCTION {s}.period_pause()')
  before=await self.counts()
  await self.execute("WITH expiry AS(SELECT clock_timestamp()+interval '900 milliseconds' t) UPDATE {s}.sessions SET authenticated_at=expiry.t-interval '8 hours',expires_at=expiry.t FROM expiry WHERE token_hash=%s",(digest('session',token),))
  async with await self.client() as client:
   response=await client.post('/api/v1/knowledge/documents',json={**materials[0],'file_id':fid,'key':str(uuid4())},headers={**self.headers(0,'application/json'),'cookie':'__Host-eai-session='+token,'x-csrf-token':csrf})
   require(response.status_code in (401,503),'outer-admission-finish-expiry')
  require(await self.counts()==before,'entire-admission-rollback')
  await self.execute('DROP TRIGGER period_pause ON {s}.document_versions;DROP FUNCTION {s}.period_pause()')
  self.passed('expiry_during_metadata_write_rolls_back_document_version_job_acl_and_request_atomically')


def main():
 logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/bd-fe-02/period-proof.json'));args=parser.parse_args()
 if not args.live:parser.error('Explicit --live required.')
 try:
  settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=18,operation='period'))
  args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result);print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
 except Exception as error:print('Knowledge period verification failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None

if __name__=='__main__':main()
