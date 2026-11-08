"""User-run FILE-01 canary: actual S3/GuardDuty + disposable PG + isolated parser.

No deployment or IAM mutation is implemented. Prepare writes local parameters only.
"""
import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import re
import threading
import time
from uuid import uuid4
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError,BotoCoreError
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings,load_settings
from enterprise_ai_platform.file_check import Harness,CheckFailure,require
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase,direct_connection,parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import migrate
from enterprise_ai_platform.infrastructure.database.roles import provision_role,drop_probe_schema
from enterprise_ai_platform.infrastructure.storage.s3 import S3Files,S3_CONFIG
from enterprise_ai_platform.infrastructure.storage.parser import DockerFileParser
from enterprise_ai_platform.modules.files.domain import FileFailure
from enterprise_ai_platform.modules.files.service import FilesService,FileInspection
from enterprise_ai_platform.modules.identity.service import digest

ROOT=Path.cwd()
TEMPLATE=Path('infra/aws/files_canary.json')
SDK=Config(connect_timeout=3,read_timeout=5,retries={'total_max_attempts':1})
REGION='ap-southeast-1'


def resolve(value,mapping):
    if isinstance(value,list):return [resolve(v,mapping) for v in value]
    if not isinstance(value,dict):return value
    if set(value)=={'Ref'}:return mapping[value['Ref']]
    if set(value)=={'Fn::GetAtt'}:return mapping['.'.join(value['Fn::GetAtt'])]
    if set(value)=={'Fn::Sub'}:return re.sub(r'\$\{([^}]+)\}',lambda m:mapping[m[1]],value['Fn::Sub'])
    return {k:resolve(v,mapping) for k,v in value.items()}


def prepare():
    previous=json.loads(Path('local/spike03/setup.json').read_text(encoding='utf-8'))
    path=Path('local/file01/cloud-setup.json');require(not path.exists(),'setup_already_exists')
    name='eai-files-canary-'+uuid4().hex[:12]
    values=dict(region=REGION,stack=name,bucket=name,operator=previous['operator'],template_sha256=hashlib.sha256(TEMPLATE.read_bytes()).hexdigest())
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(values,indent=2)+'\n',encoding='utf-8')
    path.with_name('canary-parameters.json').write_text(json.dumps([dict(ParameterKey='BucketName',ParameterValue=name),
        dict(ParameterKey='OperatorPrincipalArn',ParameterValue=values['operator'])],indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(prepared=True,aws_mutated=False,resources=5,region=REGION,stack_name=name)))


def binding(session,state):
    require(state['region']==REGION and state['bucket']==state['stack'] and re.fullmatch('eai-files-canary-[a-f0-9]{12}',state['stack']) is not None,'cloud_scope')
    require(state['template_sha256']==hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(),'template_changed')
    account=session.client('sts',config=SDK).get_caller_identity()['Account'];require(account==state['operator'].split(':')[4],'account_scope')
    cloud=session.client('cloudformation',config=SDK);stack=cloud.describe_stacks(StackName=state['stack'])['Stacks'][0]
    require(stack['StackStatus'] in {'CREATE_COMPLETE','UPDATE_COMPLETE'} and any(t['Key']=='Scope' and t['Value']=='FILE-01' for t in stack['Tags']),'stack_scope')
    require({p['ParameterKey']:p['ParameterValue'] for p in stack['Parameters']}=={'BucketName':state['bucket'],'OperatorPrincipalArn':state['operator']},'stack_parameters')
    expected=json.loads(TEMPLATE.read_text(encoding='utf-8'));actual=cloud.get_template(StackName=state['stack'],TemplateStage='Original')['TemplateBody']
    if isinstance(actual,str):actual=json.loads(actual)
    require(actual==expected,'template_drift')
    outputs={o['OutputKey']:o['OutputValue'] for o in stack['Outputs']}
    require(outputs['Region']==REGION and outputs['BucketName']==state['bucket'] and outputs['Account']==account,'outputs')
    owner=session.client('s3',config=S3_CONFIG)
    require(all(owner.get_public_access_block(Bucket=state['bucket'])['PublicAccessBlockConfiguration'].values()),'public_bucket')
    require(owner.get_bucket_versioning(Bucket=state['bucket']).get('Status')=='Enabled','versioning')
    require(owner.get_bucket_location(Bucket=state['bucket'])['LocationConstraint']==REGION,'region')
    require(owner.get_bucket_ownership_controls(Bucket=state['bucket'])['OwnershipControls']['Rules']==[{'ObjectOwnership':'BucketOwnerEnforced'}],'ownership')
    require(owner.get_bucket_encryption(Bucket=state['bucket'])['ServerSideEncryptionConfiguration']['Rules'][0]['ApplyServerSideEncryptionByDefault']['SSEAlgorithm']=='AES256','encryption')
    mapping={'AWS::AccountId':account,'AWS::Region':REGION,'AWS::Partition':'aws','Bucket':state['bucket'],'BucketName':state['bucket'],
        'Bucket.Arn':'arn:aws:s3:::'+state['bucket'],'OperatorPrincipalArn':state['operator'],'FileRole.Arn':outputs['FileRoleArn'],'ScanRole.Arn':outputs['ScanRoleArn']}
    require(json.loads(owner.get_bucket_policy(Bucket=state['bucket'])['Policy'])==resolve(expected['Resources']['BucketPolicy']['Properties']['PolicyDocument'],mapping),'bucket_policy_drift')
    iam=session.client('iam',config=SDK)
    for name in ('FileRole','ScanRole'):
        role_name=outputs[name+'Arn'].rsplit('/',1)[1];role=iam.get_role(RoleName=role_name)['Role'];spec=expected['Resources'][name]['Properties']
        require(role['AssumeRolePolicyDocument']==resolve(spec['AssumeRolePolicyDocument'],mapping),'trust_drift')
        require(not iam.list_attached_role_policies(RoleName=role_name)['AttachedPolicies'],'managed_policy_drift')
        policy=spec['Policies'][0]
        require(iam.list_role_policies(RoleName=role_name)['PolicyNames']==[policy['PolicyName']],'inline_policy_count')
        require(iam.get_role_policy(RoleName=role_name,PolicyName=policy['PolicyName'])['PolicyDocument']==resolve(policy['PolicyDocument'],mapping),'inline_policy_drift')
    plan=session.client('guardduty',config=SDK).get_malware_protection_plan(MalwareProtectionPlanId=outputs['MalwarePlanId'])
    require(plan['Status']=='ACTIVE' and plan['Role']==outputs['ScanRoleArn'] and plan['Actions']['Tagging']['Status']=='ENABLED','scan_plan')
    require(plan['ProtectedResource']=={'S3Bucket':{'BucketName':state['bucket'],'ObjectPrefixes':['quarantine/']}},'scan_prefix')
    credentials=session.client('sts',config=SDK).assume_role(RoleArn=outputs['FileRoleArn'],RoleSessionName='file01-canary',DurationSeconds=900)['Credentials']
    client=boto3.client('s3',region_name=REGION,config=S3_CONFIG,endpoint_url='https://s3.ap-southeast-1.amazonaws.com',
        aws_access_key_id=credentials['AccessKeyId'],aws_secret_access_key=credentials['SecretAccessKey'],aws_session_token=credentials['SessionToken'])
    return owner,client


class Recorder:
    def __init__(self,client,owner,bucket,path):
        self.client,self.owner,self.bucket,self.path=client,owner,bucket,path
        self.tenants=set();self.keys=set();self.lock=threading.Lock();self.ambiguous=False
        self.cleanup_status='pending';self.diagnostics=[]
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.open('x',encoding='utf-8') as stream:stream.write('{}\n')
        self.save()
    def __getattr__(self,name):
        method=getattr(self.client,name)
        if name not in {'list_object_versions','get_object_tagging','get_object_attributes','get_object'}:return method
        def observed(**values):
            event=dict(operation=name)
            try:
                response=method(**values)
                event['outcome']='ok'
                if name=='get_object_attributes':
                    event['size_present']=isinstance(response.get('ObjectSize'),int)
                    event['sha256_present']=isinstance(response.get('Checksum',{}).get('ChecksumSHA256'),str)
                return response
            except ClientError as error:
                code=error.response.get('Error',{}).get('Code','unknown')
                event.update(outcome='provider_error',provider_code=code if re.fullmatch('[A-Za-z0-9_]{1,64}',code) else 'unknown')
                raise
            except Exception:
                event['outcome']='sdk_error'
                raise
            finally:
                with self.lock:
                    self.diagnostics.append(event)
                    self.diagnostics=self.diagnostics[-32:]
        return observed
    def put_object(self,**values):
        parts=values['Key'].split('/')
        require(values['Bucket']==self.bucket and len(parts)==3 and parts[0]=='quarantine' and parts[1] in self.tenants and re.fullmatch('[a-f0-9-]{36}',parts[2]) is not None,'write_scope')
        with self.lock:
            self.keys.add(values['Key']);self.save()
        try:
            result=self.client.put_object(**values)
            if result.get('ResponseMetadata',{}).get('RetryAttempts',0):self.ambiguous=True
            return result
        except ClientError as error:
            if error.response.get('ResponseMetadata',{}).get('RetryAttempts',0) or error.response.get('Error',{}).get('Code') not in {'AccessDenied','PreconditionFailed'}:self.ambiguous=True
            raise
        except (BotoCoreError,OSError,TimeoutError):self.ambiguous=True;raise
    def save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True);self.path.write_text(json.dumps(dict(bucket=self.bucket,keys=sorted(self.keys),tenants=sorted(self.tenants),ambiguous=self.ambiguous,cleanup_status=self.cleanup_status),indent=2)+'\n',encoding='utf-8')
    def cleanup(self):
        failed=False
        for key in self.keys:
            try:
                page=self.owner.list_object_versions(Bucket=self.bucket,Prefix=key,MaxKeys=20)
                if page.get('IsTruncated'):failed=True;continue
                for item in [*page.get('Versions',[]),*page.get('DeleteMarkers',[])]:
                    if item['Key']==key:self.owner.delete_object(Bucket=self.bucket,Key=key,VersionId=item['VersionId'])
            except Exception:failed=True
        self.cleanup_status='unresolved' if self.ambiguous else 'failed' if failed else 'completed'
        self.save()
        return self.cleanup_status


class Store:
    def __init__(self,adapter):self.adapter=adapter;self.lost_once=False;self.last=None;self.read_hook=None;self.recovery_response=None;self.recovery_storage=None
    async def upload(self,key,data,digest):
        version=await self.adapter.upload(key,data,digest);self.last=dict(object_key=key,object_version=version)
        if self.lost_once:self.lost_once=False;raise FileFailure(503)
        return version
    async def recover(self,row):
        try:
            value=await self.adapter.recover(row)
            self.recovery_storage=dict(outcome='found' if value is not None else 'not_found_or_not_clean')
            return value
        except FileFailure as error:
            self.recovery_storage=dict(outcome='failed',http_status=error.status)
            raise
    async def scan(self,row):return await self.adapter.scan(row)
    async def read(self,row):
        result=await self.adapter.read(row)
        if self.read_hook:await self.read_hook()
        return result


async def cases(h):
    async def complete(file_id,expected):
        start=time.monotonic()
        while time.monotonic()-start<90:
            row=(await h.get(file_id)).json()
            if row['scan_state']!='quarantine':require(row['scan_state']==expected,'final_file_state');return
            claim=await h.scheduler.claim()
            if claim['status']=='ok':await h.inspector.process(claim)
            await asyncio.sleep(1)
        raise CheckFailure('file_processing_deadline')
    clean=await h.upload();require(clean.status_code==202,'cloud-upload');file_id=clean.json()['id']
    require((await h.get(file_id,'content')).status_code==404,'cloud-quarantine')
    h.passed('actual_s3_upload_enters_durable_quarantine')
    await complete(file_id,'ready');h.passed('actual_guardduty_and_parser_make_exact_file_ready')
    downloaded=await h.get(file_id,'content');require(downloaded.status_code==200 and downloaded.content==b'Fictional policy.','cloud-bytes')
    h.passed('actual_versioned_s3_proxy_download_hash_and_bytes_match')
    for n in (1,2,3):require((await h.get(file_id,'content',n)).status_code==404,'cloud-cross-scope')
    h.passed('actual_provider_path_keeps_colleague_tenant_and_admin_denied')
    person=h.people[0]
    async def revoke():await h.execute('UPDATE {s}.sessions SET revoked_at=clock_timestamp() WHERE token_hash=%s',(digest('session',person['token']),))
    h.store.read_hook=revoke
    require((await h.get(file_id,'content')).status_code==401,'cloud-revoke-after-read');h.store.read_hook=None
    person['token'],person['csrf']=await h.session(person['tenant'],person['person'])
    h.passed('revocation_during_actual_s3_read_blocks_buffered_output')
    require((await h.get(file_id,'content')).status_code==200,'cloud-new-session')
    h.passed('same_file_access_survives_authorized_session_replacement')
    eicar=b'X5O!P%@AP[4\\PZX54(P^)7CC)7}$'+b'EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*'
    threat=await h.upload(eicar);require(threat.status_code==202,'cloud-antivirus-upload')
    await complete(threat.json()['id'],'rejected')
    require((await h.get(threat.json()['id'],'content')).status_code==404,'cloud-antivirus-content')
    h.passed('actual_antivirus_test_is_rejected_and_never_downloaded')
    key=str(uuid4());h.store.lost_once=True
    require((await h.upload(key=key)).status_code==503,'cloud-loss-injection')
    for _ in range(90):
        if await h.store.scan(h.store.last)=='clean':break
        await asyncio.sleep(1)
    else:raise CheckFailure('recovery_scan_deadline')
    recovered=await h.upload(key=key)
    recovery=dict(http_status=recovered.status_code)
    if recovered.status_code==202:
        body=recovered.json();recovery.update(version=body.get('version'),scan_state=body.get('scan_state'))
    h.store.recovery_response=recovery
    require(recovered.status_code==202 and recovered.json()['version']==2,'cloud-metadata-recovery')
    await complete(recovered.json()['id'],'ready')
    require((await h.get(recovered.json()['id'],'content')).content==b'Fictional policy.','cloud-recovered-bytes')
    h.passed('real_put_before_lost_application_reply_recovers_checksum_version_and_job')


async def live(settings,state,image,output):
    for prior in output.parent.glob('cloud-owned-intents*.json'):
        previous=json.loads(prior.read_text(encoding='utf-8'))
        require(not previous.get('keys') or previous.get('cleanup_status')=='completed','prior_owned_intents_need_reconciliation')
    session=boto3.Session(region_name=REGION);owner,client=binding(session,state)
    schema='db03_probe_'+uuid4().hex
    recorder=Recorder(client,owner,state['bucket'],output.with_name('cloud-owned-intents-'+schema.rsplit('_',1)[1]+'.json'))
    adapter=S3Files(recorder,state['bucket']);parser=DockerFileParser(image);database=None;owned=False
    direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);base=parse_target(settings.database_url.get_secret_value());verify_same_database(base,direct)
    result=dict(passed=False,checks=[],storage_provider='actual_AWS_S3_GuardDuty',parser='actual_isolated_Docker',public_deployment=False,platform_schema_modified=False)
    try:
        before=await migrate(direct)
        async with direct_connection(direct) as connection:require((await (await connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone()) is None,'namespace')
        owned=True;await migrate(direct,schema=schema,upgrade=True)
        target=await provision_role(direct,base,schema=schema,role_name='eai_runtime_'+schema.rsplit('_',1)[1]);database=RuntimeDatabase(target,schema=schema);await database.open()
        async with direct_connection(direct,timeout=600) as connection:
            h=Harness(connection,database,schema,parser);result['checks']=h.checks;await h.setup_files();recorder.tenants={str(p['tenant']) for p in h.people};recorder.save()
            h.store=Store(adapter);h.files=FilesService(h.file_repo,h.store,'https://file-fixture.example.test');h.inspector=FileInspection(h.file_repo,h.store,parser,h.jobs,h.jobs._credential_hash)
            await cases(h);result.update(checks=h.checks,check_count=len(h.checks),tested_revision=8,region=REGION,platform_revision=before[0],template_sha256=state['template_sha256'])
        require(await migrate(direct)==before,'platform_unchanged')
        result['passed']=True
    finally:
        database_cleanup='completed'
        try:
            await parser.close();await adapter.close()
            if database is not None:await database.close()
        except Exception:database_cleanup='failed'
        try:
            if owned:await drop_probe_schema(direct,schema)
        except Exception:database_cleanup='failed'
        cleanup=recorder.cleanup();result['owned_object_cleanup']=cleanup;result['disposable_database_cleanup']='completed';result['passed']=result['passed'] and cleanup=='completed'
        result['disposable_database_cleanup']=database_cleanup;result['passed']=result['passed'] and database_cleanup=='completed'
        result['check_count']=len(result['checks'])
        result['storage_diagnostics']=recorder.diagnostics
        if 'h' in locals() and isinstance(h.store,Store):
            result['recovery_response']=h.store.recovery_response
            result['recovery_storage']=h.store.recovery_storage
        output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    require(result['passed'],'canary_failed')
    print(json.dumps(dict(passed=True,check_count=result['check_count'],owned_object_cleanup='completed',resource_cleanup='user_followup_required')))


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['prepare','run']);parser.add_argument('--live',action='store_true')
    parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--setup',type=Path,default=Path('local/file01/cloud-setup.json'));parser.add_argument('--output',type=Path,default=Path('local/file01/cloud-canary.json'));args=parser.parse_args()
    try:
        if args.command=='prepare':prepare();return
        require(args.live,'explicit_live_required');args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text('{"passed":false,"status":"running"}\n',encoding='utf-8')
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings);state=json.loads(args.setup.read_text(encoding='utf-8'));image=json.loads(Path('local/spike03/parser-image.json').read_text(encoding='utf-8'))['image_id']
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:runner.run(live(settings,state,image,args.output))
    except Exception as error:
        fields=dict(passed=False,code=str(error) if isinstance(error,CheckFailure) else 'sanitized_canary_failure')
        if isinstance(error,ClientError):fields.update(operation=getattr(error,'operation_name','unknown'),provider_code=error.response.get('Error',{}).get('Code','unknown'))
        if args.command=='run':
            try:
                record=json.loads(args.output.read_text(encoding='utf-8'));record.update(passed=False,status='failed',code=fields['code']);args.output.write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
            except Exception:pass
        print(json.dumps(fields));raise SystemExit(1) from None


if __name__=='__main__':main()
