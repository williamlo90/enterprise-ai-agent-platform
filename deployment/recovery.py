"""Operator backup/restore/replay CLI. No automatic live release or financial replay."""
import argparse,asyncio,hashlib,json,logging,os,secrets,subprocess,time
from pathlib import Path
from uuid import UUID,uuid4
import boto3,psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from enterprise_ai_platform.integrations.security.recovery_journal import RecoveryJournal

def save(p,x):
 p.write_text(json.dumps(x,indent=2)+'\n',encoding='utf-8');p.chmod(0o600)
def command_env(url):
 d=conninfo_to_dict(url)
 if d.get('sslmode')!='verify-full':raise ValueError('TLS verify-full required')
 env=dict(os.environ)
 for name,key in [('PGHOST','host'),('PGPORT','port'),('PGUSER','user'),('PGPASSWORD','password'),('PGDATABASE','dbname')]:env[name]=d.get(key,'5432' if key=='port' else '')
 env['PGSSLMODE']='verify-full';env['PGCHANNELBINDING']='require'
 import certifi
 env['PGSSLROOTCERT']=certifi.where();return env,d

def dump(url,name,directory):
 env,target=command_env(url);out=directory/(name+'.dump')
 subprocess.run(['pg_dump','--format=custom','--no-owner','--file',str(out),'--schema',name],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=600)
 with psycopg.connect(url) as c:
  if name=='platform':
   rows=c.execute('SELECT role_name::text FROM platform.runtime_role_registry UNION SELECT role_name::text FROM platform.runtime_access_role_registry').fetchall()
  else:rows=c.execute('SELECT role_name::text FROM reimbursement.runtime_binding').fetchall()
  roles=[]
  for row in rows:
   role=row[0];login=c.execute('SELECT rolcanlogin FROM pg_roles WHERE rolname=%s',(role,)).fetchone()[0]
   parents=[v[0] for v in c.execute('SELECT p.rolname FROM pg_auth_members m JOIN pg_roles p ON p.oid=m.roleid JOIN pg_roles child ON child.oid=m.member WHERE child.rolname=%s',(role,))]
   roles.append(dict(name=role,login=login,parents=parents))
 return dict(schema=name,source_host=target['host'],source_database=target['dbname'],sha256=hashlib.sha256(out.read_bytes()).hexdigest(),roles=roles)

async def backup(config,directory):
 if config.get('producers_quiesced') is not True:raise ValueError('Quiesce producers before consistent backup')
 directory.mkdir(parents=True,exist_ok=False);directory.chmod(0o700)
 reader=RecoveryJournal(config['reader_arn'],config['journal_id'],'reader');s3=boto3.client('s3',region_name='ap-southeast-1')
 manifest=dict(version=1,backup_id=str(uuid4()),source_files_bucket=config['files_bucket'],created_at=time.time(),journal_id=config['journal_id'],files=[],databases=[],secrets=[],watermarks={})
 try:
  with psycopg.connect(config['platform_url']) as c:tenants=[str(r[0]) for r in c.execute('SELECT id FROM platform.tenants')]
  # Cursor precedes snapshots; a concurrent mutation is replayed conservatively.
  for t in tenants:
   head=await reader.head(t);manifest['watermarks'][t]={k:head[k] for k in ('journal_id','seq','hash')}
  prefix='backup/'+manifest['backup_id']+'/'
  for name,key in [('platform','platform_url'),('reimbursement','service_url')]:
   entry=dump(config[key],name,directory);entry['key']=prefix+name+'.dump';manifest['databases'].append(entry)
   s3.upload_file(str(directory/(name+'.dump')),config['backup_bucket'],entry['key'],ExtraArgs={'ServerSideEncryption':'AES256'})
  # Pin the exact owning versions in this snapshot, not a live bucket listing.
  with psycopg.connect(config['platform_url']) as c:files=c.execute("SELECT id,object_key,object_version,digest FROM platform.files WHERE object_version IS NOT NULL").fetchall()
  if len(files)>config.get('max_files',1000):raise ValueError('Explicit backup file bound exceeded')
  for fid,key,version,digest in files:
   if not version:raise ValueError('Unversioned file is not a backup')
   destination=prefix+'files/'+str(fid)
   s3.copy_object(Bucket=config['backup_bucket'],Key=destination,CopySource={'Bucket':config['files_bucket'],'Key':key,'VersionId':version},ServerSideEncryption='AES256')
   copied=s3.head_object(Bucket=config['backup_bucket'],Key=destination)
   manifest['files'].append(dict(id=str(fid),key=key,source_version=version,digest=digest,backup_key=destination,backup_version=copied['VersionId']))
  sm=boto3.client('secretsmanager',region_name='ap-southeast-1')
  for arn in config.get('secret_arns',[]):
   secret=sm.get_secret_value(SecretId=arn);key=prefix+'config/'+hashlib.sha256(arn.encode()).hexdigest()+'.json'
   s3.put_object(Bucket=config['backup_bucket'],Key=key,Body=json.dumps({'arn':arn,'version_id':secret['VersionId'],'value':secret.get('SecretString'),'binary':secret.get('SecretBinary').hex() if secret.get('SecretBinary') else None}).encode(),ServerSideEncryption='AES256')
   manifest['secrets'].append(dict(arn=arn,version_id=secret['VersionId'],backup_key=key))
  save(directory/'manifest.json',manifest)
  s3.upload_file(str(directory/'manifest.json'),config['backup_bucket'],prefix+'manifest.json',ExtraArgs={'ServerSideEncryption':'AES256'})
  return dict(status='backup_written',manifest_key=prefix+'manifest.json')
 finally:reader.close();s3.close()

def restore(config,manifest,directory):
 # Only a new, operator-named database on a DIFFERENT host is supported.
 directory.mkdir(parents=True,exist_ok=False);directory.chmod(0o700);s3=boto3.client('s3',region_name='ap-southeast-1');credentials={}
 if not config['restore_files_bucket'].startswith('eai-restore-') or config['restore_files_bucket']==manifest['source_files_bucket']:raise ValueError('Isolated files bucket required')
 for item in manifest['databases']:
  name=item['schema'];url=config['restore_platform_url' if name=='platform' else 'restore_service_url'];env,target=command_env(url)
  if not target['dbname'].startswith('eai_restore_') or target['host']==item['source_host']:raise ValueError('Refusing nonisolated restore target')
  with psycopg.connect(url,autocommit=True) as c:
   if c.execute('SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)',(name,)).fetchone()[0]:raise ValueError('Restore schema already exists')
   for descriptor in item['roles']:
    role=descriptor['name']
    if c.execute('SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=%s)',(role,)).fetchone()[0]:raise ValueError('Dedicated target needs fresh application roles')
    password=secrets.token_urlsafe(32);credentials[role]=password
    # Runtime access groups are reconstructed as NOLOGIN, application logins
    # get fresh credentials. No old password hash is restored.
    login=descriptor['login']
    c.execute(sql.SQL('CREATE ROLE {} '+('LOGIN PASSWORD {}' if login else 'NOLOGIN')+' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS').format(sql.Identifier(role),*[sql.Literal(password)] if login else []))
  with psycopg.connect(url,autocommit=True) as c:
   for descriptor in item['roles']:
    for parent in descriptor['parents']:
     if parent not in {r['name'] for r in item['roles']}:raise ValueError('Unexpected source role membership')
     c.execute(sql.SQL('GRANT {} TO {} WITH INHERIT TRUE, SET FALSE, ADMIN FALSE').format(sql.Identifier(parent),sql.Identifier(descriptor['name'])))
  path=directory/(name+'.dump');s3.download_file(config['backup_bucket'],item['key'],str(path))
  if hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('Backup digest mismatch')
  subprocess.run(['pg_restore','--dbname',target['dbname'],'--no-owner','--exit-on-error','--single-transaction',str(path)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=600)
  with psycopg.connect(url) as c:
   if name=='platform':
    c.execute('UPDATE platform.runtime_role_registry b SET role_oid=r.oid FROM pg_roles r WHERE b.role_name=r.rolname')
    c.execute('UPDATE platform.runtime_access_role_registry b SET role_oid=r.oid FROM pg_roles r WHERE b.role_name=r.rolname')
    c.execute('UPDATE platform.security_restore_state SET held=true,restore_id=%s',(manifest['backup_id'],))
    c.execute("UPDATE platform.outbox_jobs SET state='held',generation=generation+1,worker_id=NULL,lease_until=NULL,attempt_deadline=NULL WHERE state NOT IN ('completed','dead')")
   else:c.execute('UPDATE reimbursement.runtime_binding b SET role_oid=r.oid FROM pg_roles r WHERE b.role_name=r.rolname')
 # Restore bytes, then rebind physical versions under held administrative scope.
 mapped=[]
 for f in manifest['files']:
  x=s3.copy_object(Bucket=config['restore_files_bucket'],Key=f['key'],CopySource={'Bucket':config['backup_bucket'],'Key':f['backup_key'],'VersionId':f['backup_version']},ServerSideEncryption='AES256')
  if not x.get('VersionId'):raise ValueError('Restore bucket versioning required')
  body=s3.get_object(Bucket=config['restore_files_bucket'],Key=f['key'],VersionId=x['VersionId'])['Body'];digest=hashlib.sha256();size=0
  try:
   for block in iter(lambda:body.read(65536),b''):
    size+=len(block)
    if size>10*1024*1024:raise ValueError('Unexpected file size')
    digest.update(block)
  finally:body.close()
  if digest.hexdigest()!=f['digest']:raise ValueError('Restored byte digest mismatch')
  mapped.append((x['VersionId'],f['id'],f['digest']))
 with psycopg.connect(config['restore_platform_url']) as c:
  # Only USER triggers are disabled inside this held transaction; FK/check
  # constraints remain active. This is never a live mutation API.
  c.execute('ALTER TABLE platform.files DISABLE TRIGGER USER');c.execute('ALTER TABLE platform.document_versions DISABLE TRIGGER USER')
  for version,fid,digest in mapped:
   row=c.execute('UPDATE platform.files SET object_version=%s WHERE id=%s AND digest=%s RETURNING id',(version,fid,digest)).fetchone()
   if row is None:raise ValueError('Restored file identity mismatch')
   c.execute('UPDATE platform.document_versions SET object_version=%s WHERE file_id=%s AND file_digest=%s',(version,fid,digest))
  c.execute('ALTER TABLE platform.files ENABLE TRIGGER USER');c.execute('ALTER TABLE platform.document_versions ENABLE TRIGGER USER')
 save(directory/'fresh-runtime-passwords.json',credentials)
 save(directory/'restore-state.json',{'held':True,'financial_replay':False,'config_secrets_restored':False,'file_versions_rebound':len(mapped)})
 s3.close();return dict(status='restored_held',credentials_file=str(directory/'fresh-runtime-passwords.json'))

def guard_restore_targets(config,manifest):
 UUID(manifest['backup_id'])
 for item in manifest['databases']:
  _,target=command_env(config['restore_platform_url' if item['schema']=='platform' else 'restore_service_url'])
  if not target['dbname'].startswith('eai_restore_') or target['host']==item['source_host']:raise ValueError('Refusing nonisolated recovery mutation')
 with psycopg.connect(config['restore_platform_url']) as c:
  marker=c.execute('SELECT held,restore_id FROM platform.security_restore_state').fetchone()
  if not marker or not marker[0] or str(marker[1])!=manifest['backup_id']:raise ValueError('Target is not the held restore from this manifest')

async def replay(config,manifest,directory):
 guard_restore_targets(config,manifest)
 directory.mkdir(parents=True,exist_ok=False);directory.chmod(0o700);reader=RecoveryJournal(config['reader_arn'],config['journal_id'],'reader')
 try:
  for t,cursor in manifest['watermarks'].items():
   result=await reader.replay(t,cursor)
   with psycopg.connect(config['restore_platform_url']) as c:
    c.execute('UPDATE platform.security_restore_state SET held=true')
    for d in result['deny']:
     c.execute('INSERT INTO platform.security_restore_denies(tenant_id,kind,resource_id,seq) VALUES(%s,%s,%s,%s) ON CONFLICT(tenant_id,kind,resource_id) DO UPDATE SET seq=greatest(platform.security_restore_denies.seq,EXCLUDED.seq),applied=false',(t,d['kind'],d['id'],result['seq']))
    c.execute('UPDATE platform.sessions SET revoked_at=coalesce(revoked_at,clock_timestamp())')
    c.execute('DELETE FROM platform.login_transactions')
    c.execute('DELETE FROM platform.identity_jwks')
    c.execute('DELETE FROM platform.identity_negative_keys')
    for table in ('delegation_keys','internal_caller_credentials','job_callers','policy_receiver_keys','receipt_receiver_keys'):
     c.execute(sql.SQL('UPDATE platform.{} SET active=false WHERE active').format(sql.Identifier(table)))

    # Scope quarantine never invents business state or replays a transaction.
    for d in result['deny']:
     kind,rid=d['kind'],d['id']
     if kind=='principal':c.execute('UPDATE platform.principals SET active=false WHERE id=%s',(rid,))
     elif kind=='membership':c.execute('UPDATE platform.memberships SET active=false,access_revision=access_revision+1 WHERE tenant_id=%s AND id=%s AND active',(t,rid))
     elif kind=='document':
      c.execute('UPDATE platform.documents SET withdrawn_at=coalesce(withdrawn_at,clock_timestamp()),active_version_id=NULL,access_revision=access_revision+1 WHERE tenant_id=%s AND id=%s AND (withdrawn_at IS NULL OR active_version_id IS NOT NULL)',(t,rid))
     elif kind=='policy':c.execute("UPDATE platform.policy_packages SET allowed_for_new=false,allowed_for_locked_claims=false,publication_state='withdrawn',version=version+1,eligibility_revision=eligibility_revision+1 WHERE tenant_id=%s AND id=%s AND (publication_state<>'withdrawn' OR allowed_for_new OR allowed_for_locked_claims)",(t,rid))
     elif kind=='claim':
      with psycopg.connect(config['restore_service_url']) as service:service.execute('UPDATE reimbursement.tenant_registry SET active=false WHERE tenant_id=%s',(t,))
     elif kind=='conversation':
      # Replay an independently recorded deletion intent, not a new user grant.
      # Canonical financial records are untouched; ambiguous dependencies hold.
      if c.execute('SELECT EXISTS(SELECT 1 FROM platform.assisted_requests WHERE tenant_id=%s AND conversation_id=%s AND draft_id IS NOT NULL)',(t,rid)).fetchone()[0]:continue
      c.execute('INSERT INTO platform.privacy_erasure_contexts VALUES(pg_backend_pid(),pg_current_xact_id()::text,%s,%s) ON CONFLICT DO NOTHING',(t,rid))
      c.execute("UPDATE platform.runs SET state='failed',writer_generation=writer_generation+1,version=version+1,lease_until=NULL WHERE tenant_id=%s AND conversation_id=%s AND state IN ('queued','running','waiting','held')",(t,rid))
      for table in ('checkpoint_writes','checkpoints'):
       c.execute(sql.SQL('DELETE FROM platform.{} cp USING platform.runs r WHERE cp.tenant_id=r.tenant_id AND cp.run_id=r.id AND r.tenant_id=%s AND r.conversation_id=%s').format(sql.Identifier(table)),(t,rid))
      for table in ('rag_citations','feedback','assisted_requests'):
       c.execute(sql.SQL('DELETE FROM platform.{} WHERE tenant_id=%s AND conversation_id=%s').format(sql.Identifier(table)),(t,rid))
      c.execute("UPDATE platform.messages SET body='[redacted]' WHERE tenant_id=%s AND conversation_id=%s",(t,rid))
      c.execute("UPDATE platform.conversation_requests SET response=jsonb_build_object('id',conversation_id,'privacy_redacted',true) WHERE tenant_id=%s AND conversation_id=%s",(t,rid))
      c.execute('UPDATE platform.conversations SET manual_name=NULL,active_run_id=NULL,version=version+1 WHERE tenant_id=%s AND id=%s',(t,rid))
      c.execute('DELETE FROM platform.privacy_erasure_contexts WHERE backend_pid=pg_backend_pid() AND transaction_id=pg_current_xact_id()::text AND tenant_id=%s AND conversation_id=%s',(t,rid))
     c.execute('UPDATE platform.security_restore_denies SET applied=true WHERE tenant_id=%s AND kind=%s AND resource_id=%s',(t,kind,rid))
    c.execute('UPDATE platform.security_restore_state SET watermarks=watermarks||jsonb_build_object(%s,%s::jsonb)',(t,json.dumps({k:result[k] for k in ('journal_id','seq','hash')})))
   save(directory/(t+'.json'),result)
  return {'status':'replay_recorded_held','note':'No application release or financial replay; unresolved deletion scopes require owning reconciliation.'}
 finally:reader.close()

async def release(config,manifest,directory):
 guard_restore_targets(config,manifest)
 if config.get('producers_quiesced') is not True:raise ValueError('All producers must remain stopped')
 directory.mkdir(parents=True,exist_ok=False);directory.chmod(0o700)
 # This publishes committed recovery-source events before service can reopen.
 # It never marks a financial business operation committed or replays it.
 for source,url,arn,schema in [('platform',config['restore_platform_url'],config['platform_writer_arn'],'platform'),('reimbursement',config['restore_service_url'],config['reference_writer_arn'],'reimbursement')]:
  writer=RecoveryJournal(arn,config['journal_id'],source)
  try:
   with psycopg.connect(url) as c:
    rows=c.execute(sql.SQL('SELECT event FROM {}.security_journal_outbox ORDER BY tenant_id,event_id LIMIT 10001').format(sql.Identifier(schema))).fetchall()
   if len(rows)>10000:raise ValueError('Use a reviewed incremental outbox drain before release')
   for row in rows:
    await writer.append(row[0],'intent');await writer.append(row[0],'result')
  finally:writer.close()
 reader=RecoveryJournal(config['reader_arn'],config['journal_id'],'reader')
 try:
  with psycopg.connect(config['restore_platform_url']) as c:
   if c.execute('SELECT EXISTS(SELECT 1 FROM platform.security_restore_denies WHERE NOT applied)').fetchone()[0]:raise ValueError('Unresolved deletion/financial scope remains')
   if c.execute("SELECT EXISTS(SELECT 1 FROM platform.actions WHERE state NOT IN ('committed','not_committed'))").fetchone()[0]:raise ValueError('Reconcile financial effects through owning recovery authority')
   # Both sides of a financially terminal operation must agree. Never infer
   # no commit from absence in a differently timed service snapshot.
   actions=c.execute('SELECT tenant_id,id,canonical_hash,state,kind,actor_id FROM platform.actions').fetchall()
   with psycopg.connect(config['restore_service_url']) as service:
    operations={(str(r[0]),str(r[1])):r[2:] for r in service.execute('SELECT tenant_id,operation_id,request_hash,state,kind,actor_id FROM reimbursement.service_operations')}
   identities=set()
   for tenant,operation,digest,state,kind,actor in actions:
    identity=(str(tenant),str(operation));identities.add(identity);record=operations.get(identity)
    if record is None or record[0]!=digest or record[2]!=kind or str(record[3])!=str(actor):raise ValueError('Cross-snapshot financial identity remains unresolved')
    if (state=='committed')!=(record[1]=='committed'):raise ValueError('Cross-snapshot financial outcome remains unresolved')
   if set(operations)-identities:raise ValueError('Service-side orphan operation remains unresolved')
   marks=c.execute('SELECT watermarks FROM platform.security_restore_state').fetchone()[0]
   for t,mark in marks.items():
    head=await reader.head(t)
    if head['journal_id']!=mark['journal_id'] or head['seq']!=mark['seq'] or head['hash']!=mark['hash']:raise ValueError('Journal advanced: replay the new tail before release')
   c.execute('UPDATE platform.security_restore_state SET held=false')
  save(directory/'release.json',{'status':'gate_released','watermarks':marks,'workers_started':False,'service_claim_quarantines_preserved':True})
  return {'status':'gate_released_workers_still_stopped'}
 finally:reader.close()

async def main():
 logging.disable(logging.CRITICAL)
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('operation',choices=['backup','restore','replay','release']);p.add_argument('--config',type=Path,required=True);p.add_argument('--manifest',type=Path);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
 config=json.loads(a.config.read_text(encoding='utf-8'))
 if a.operation=='backup':result=await backup(config,a.out)
 elif a.operation=='release':result=await release(config,json.loads(a.manifest.read_text(encoding='utf-8')),a.out)
 else:
  manifest=json.loads(a.manifest.read_text(encoding='utf-8'))
  if manifest['journal_id']!=config['journal_id']:raise ValueError('Journal epoch mismatch')
  result=restore(config,manifest,a.out) if a.operation=='restore' else await replay(config,manifest,a.out)
 print(json.dumps(result))
if __name__=='__main__':
 try:asyncio.run(main())
 except Exception:raise SystemExit('Recovery operation stopped; target remains held. Inspect local configuration privately.') from None
