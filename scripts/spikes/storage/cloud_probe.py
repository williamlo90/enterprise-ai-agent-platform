"""User-run SPIKE-03 cloud probe. No deploy/IAM/plan mutation is implemented."""
import argparse
import base64
from contextlib import closing
import hashlib
import io
import json
import logging
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from file_policy import MAX_BYTES, SCAN_TAG, inspect_bytes, require_same_bytes, scan_ready

ROOT=Path(__file__).resolve().parents[3]
TEMPLATE=ROOT/'infra/aws/storage_spike.json'
REGION='ap-southeast-1'
SDK=Config(connect_timeout=3,read_timeout=10,retries={'total_max_attempts':1},s3={'addressing_style':'virtual'})


class ProbeRejected(ValueError):pass


def require(value,code):
    if not value:raise ProbeRejected(code)


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'))


def template_hash():return hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()


def prepare(operator,output):
    require(re.fullmatch(r'arn:aws:iam::[0-9]{12}:(user|role)/[A-Za-z0-9_+=,.@/-]{1,256}',operator) is not None,'operator_arn')
    require(not output.exists(),'setup_already_exists')
    suffix=uuid4().hex[:12]
    state=dict(region=REGION,stack='eai-storage-spike-'+suffix,bucket='eai-storage-spike-'+suffix,operator=operator,template_sha256=template_hash())
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(state,indent=2)+'\n',encoding='utf-8')
    output.with_name('parameters.json').write_text(json.dumps([
        dict(ParameterKey='BucketName',ParameterValue=state['bucket']),dict(ParameterKey='OperatorPrincipalArn',ParameterValue=operator)],indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(prepared=True,aws_mutated=False,region=REGION,resources=8,stack_name=state['stack'])))


def resolve(value,mapping):
    if isinstance(value,list):return [resolve(x,mapping) for x in value]
    if not isinstance(value,dict):return value
    if set(value)=={'Ref'}:return mapping[value['Ref']]
    if set(value)=={'Fn::GetAtt'}:return mapping['.'.join(value['Fn::GetAtt'])]
    if set(value)=={'Fn::Sub'}:
        return re.sub(r'\$\{([^}]+)\}',lambda m:mapping[m[1]],value['Fn::Sub'])
    return {k:resolve(v,mapping) for k,v in value.items()}


def assume(session,arn):
    result=session.client('sts',region_name=REGION,config=SDK).assume_role(RoleArn=arn,RoleSessionName='spike03-fixture',DurationSeconds=900)['Credentials']
    return boto3.client('s3',region_name=REGION,config=SDK,aws_access_key_id=result['AccessKeyId'],
                        aws_secret_access_key=result['SecretAccessKey'],aws_session_token=result['SessionToken'])


def denied(operation,code='AccessDenied'):
    try:operation()
    except ClientError as error:
        require(error.response.get('Error',{}).get('Code')==code,'unexpected_provider_denial')
        return
    raise ProbeRejected('negative_case_accepted')


class CloudFixtures:
    """Register intent before each write, including negative writes and lost replies."""
    def __init__(self,owner,bucket,run_id,manifest):
        self.owner,self.bucket,self.run_id,self.manifest=owner,bucket,run_id,manifest
        self.keys=set();self.ambiguous=False

    def write(self,client,operation,**values):
        key=values['Key']
        require(values['Bucket']==self.bucket and re.fullmatch(r'(quarantine|unscanned)/tenant-[ab]/'+self.run_id+r'/[a-z-]+',key) is not None,'write_scope')
        self.keys.add(key)
        try:
            result=getattr(client,operation)(**values)
            if result.get('ResponseMetadata',{}).get('RetryAttempts',0)>0:self.ambiguous=True
            return result
        except ClientError as error:
            if error.response.get('ResponseMetadata',{}).get('RetryAttempts',0)>0 or error.response.get('Error',{}).get('Code') not in {'AccessDenied','PreconditionFailed','ConditionalRequestConflict'}:
                self.ambiguous=True
            raise
        except (BotoCoreError,TimeoutError,OSError):
            self.ambiguous=True
            raise

    def cleanup(self):
        failed=False
        for key in sorted(self.keys):
            try:
                marker={}
                for _ in range(10):
                    page=self.owner.list_object_versions(Bucket=self.bucket,Prefix=key,MaxKeys=100,**marker)
                    for item in [*page.get('Versions',[]),*page.get('DeleteMarkers',[])]:
                        if item['Key']==key:
                            self.owner.delete_object(Bucket=self.bucket,Key=key,VersionId=item['VersionId'])
                    if not page.get('IsTruncated'):break
                    marker={'KeyMarker':page['NextKeyMarker'],'VersionIdMarker':page['NextVersionIdMarker']}
                else:failed=True
            except Exception:failed=True
        # A timed-out PUT may still arrive after listing. Keep its intent for operator
        # reconciliation rather than falsely certifying cleanup after one empty list.
        status='unresolved' if self.ambiguous else 'failed' if failed else 'completed'
        if status!='completed':
            self.manifest.parent.mkdir(parents=True,exist_ok=True)
            self.manifest.write_text(json.dumps(dict(bucket=self.bucket,run_id=self.run_id,keys=sorted(self.keys),
                status=status,template_sha256=template_hash(),instruction='Reconcile these exact run-owned keys/versions after pending requests have drained; do not empty the bucket.'),indent=2)+'\n',encoding='utf-8')
        return status


def bindings(session,state):
    require(state.get('region')==REGION and state.get('template_sha256')==template_hash(),'setup_template_binding')
    require(re.fullmatch(r'eai-storage-spike-[a-f0-9]{12}',state.get('stack','')) is not None,'stack_name')
    require(state.get('stack')==state.get('bucket'),'bucket_name')
    account=session.client('sts',region_name=REGION,config=SDK).get_caller_identity()['Account']
    require(state['operator'].split(':')[4]==account,'operator_account')
    cloud=session.client('cloudformation',region_name=REGION,config=SDK)
    stack=cloud.describe_stacks(StackName=state['stack'])['Stacks'][0]
    require(stack['StackStatus'] in {'CREATE_COMPLETE','UPDATE_COMPLETE'},'stack_not_ready')
    require(any(t['Key']=='Scope' and t['Value']=='SPIKE-03' for t in stack.get('Tags',[])),'stack_scope')
    params={p['ParameterKey']:p['ParameterValue'] for p in stack['Parameters']}
    require(params=={'BucketName':state['bucket'],'OperatorPrincipalArn':state['operator']},'stack_parameters')
    remote=cloud.get_template(StackName=state['stack'],TemplateStage='Original')['TemplateBody']
    if isinstance(remote,str):remote=json.loads(remote)
    template=json.loads(TEMPLATE.read_text(encoding='utf-8'))
    require(canonical(remote)==canonical(template),'stack_template_drift')
    outputs={p['OutputKey']:p['OutputValue'] for p in stack['Outputs']}
    require(outputs['Region']==REGION and outputs['Account']==account and outputs['BucketName']==state['bucket'],'stack_outputs')
    plan=session.client('guardduty',region_name=REGION,config=SDK).get_malware_protection_plan(MalwareProtectionPlanId=outputs['MalwarePlanId'])
    require(plan['Status']=='ACTIVE','scanner_not_active')
    require(plan['Role']==outputs['ScanRoleArn'] and plan['ProtectedResource']=={'S3Bucket':{'BucketName':state['bucket'],'ObjectPrefixes':['quarantine/']}},'scanner_binding')
    require(plan['Actions']['Tagging']['Status']=='ENABLED','scanner_tagging')
    mapping={'AWS::AccountId':account,'AWS::Region':REGION,'AWS::Partition':'aws','Bucket':state['bucket'],
             'BucketName':state['bucket'],'Bucket.Arn':'arn:aws:s3:::'+state['bucket'],'OperatorPrincipalArn':state['operator']}
    for key,value in outputs.items():
        if key.endswith('RoleArn'):mapping[key[:-3]+'.Arn']=value
    iam=session.client('iam',region_name=REGION,config=SDK)
    for name in ('UploadARole','ReadARole','UploadBRole','ReadBRole','ScanRole'):
        arn=outputs[name+'Arn'];role_name=arn.rsplit('/',1)[1]
        actual=iam.get_role(RoleName=role_name)['Role']
        expected=template['Resources'][name]['Properties']
        require(canonical(actual['AssumeRolePolicyDocument'])==canonical(resolve(expected['AssumeRolePolicyDocument'],mapping)),'role_trust_drift')
        require('PermissionsBoundary' not in actual,'unexpected_role_boundary')
        require(iam.list_attached_role_policies(RoleName=role_name)['AttachedPolicies']==[],'unexpected_managed_policy')
        policy=expected['Policies'][0]
        require(iam.list_role_policies(RoleName=role_name)['PolicyNames']==[policy['PolicyName']],'unexpected_inline_policy')
        stored=iam.get_role_policy(RoleName=role_name,PolicyName=policy['PolicyName'])['PolicyDocument']
        require(canonical(stored)==canonical(resolve(policy['PolicyDocument'],mapping)),'role_policy_drift')
    owner=session.client('s3',region_name=REGION,config=SDK)
    require(all(owner.get_public_access_block(Bucket=state['bucket'])['PublicAccessBlockConfiguration'].values()),'public_access_block')
    require(owner.get_bucket_versioning(Bucket=state['bucket']).get('Status')=='Enabled','bucket_versioning')
    require(owner.get_bucket_location(Bucket=state['bucket'])['LocationConstraint']==REGION,'bucket_region')
    require(owner.get_bucket_ownership_controls(Bucket=state['bucket'])['OwnershipControls']['Rules']==[{'ObjectOwnership':'BucketOwnerEnforced'}],'bucket_ownership')
    encryption=owner.get_bucket_encryption(Bucket=state['bucket'])['ServerSideEncryptionConfiguration']['Rules']
    require(len(encryption)==1 and encryption[0]['ApplyServerSideEncryptionByDefault']['SSEAlgorithm']=='AES256','bucket_encryption')
    bucket_policy=json.loads(owner.get_bucket_policy(Bucket=state['bucket'])['Policy'])
    require(canonical(bucket_policy)==canonical(resolve(template['Resources']['BucketPolicy']['Properties']['PolicyDocument'],mapping)),'bucket_policy_drift')
    return owner,outputs


def run(session,state,output):
    owner,outputs=bindings(session,state)
    clients={name:assume(session,outputs[name+'RoleArn']) for name in ('UploadA','ReadA','UploadB','ReadB')}
    bucket=state['bucket'];run_id=uuid4().hex;checks=[];scan_records=[]
    fixtures=CloudFixtures(owner,bucket,run_id,output.with_name('cleanup-required-'+run_id+'.json'))
    def passed(name):
        require(name not in checks,'duplicate_check');checks.append(name)
    def put(client,key,data):
        result=fixtures.write(client,'put_object',Bucket=bucket,Key=key,Body=data,IfNoneMatch='*',ContentType='application/octet-stream',
                                 ServerSideEncryption='AES256',ChecksumSHA256=base64.b64encode(hashlib.sha256(data).digest()).decode())
        require(result.get('VersionId') not in (None,'null'),'version_not_pinned')
        return dict(Key=key,VersionId=result['VersionId'])
    def get(client,ref):
        response=client.get_object(Bucket=bucket,**ref)
        with closing(response['Body']) as body:
            data=body.read(MAX_BYTES+1)
        require(len(data)<=MAX_BYTES,'download_size_limit');return data
    def wait_scan(ref):
        start=time.monotonic()
        while time.monotonic()-start<180:
            tags=owner.get_object_tagging(Bucket=bucket,**ref)['TagSet']
            status=next((t['Value'] for t in tags if t['Key']==SCAN_TAG),None)
            if status is not None:
                scan_records.append(dict(status=status,duration_seconds=round(time.monotonic()-start,3),version_binding_sha256=hashlib.sha256((ref['Key']+'\0'+ref['VersionId']).encode()).hexdigest()))
                return tags
            time.sleep(2)
        raise ProbeRejected('scanner_deadline')
    result=None
    try:
        pending=put(clients['UploadA'],'unscanned/tenant-a/'+run_id+'/pending',b'Fictional pending fixture')
        denied(lambda:get(clients['ReadA'],pending));passed('unscanned_version_denied')
        denied(lambda:clients['UploadA'].put_object_tagging(Bucket=bucket,**pending,Tagging={'TagSet':[{'Key':SCAN_TAG,'Value':'NO_THREATS_FOUND'}]}));passed('uploader_cannot_forge_clean_tag')
        data=b'Fictional policy: Travel approval required.\nKebijakan perjalanan fiktif.\n'
        identity=inspect_bytes(data,'knowledge','text/plain')
        clean=put(clients['UploadA'],'quarantine/tenant-a/'+run_id+'/clean',data)
        require(scan_ready(wait_scan(clean)),'benign_not_clean');passed('managed_scanner_benign_clean')
        downloaded=get(clients['ReadA'],clean);require_same_bytes(downloaded,identity);passed('version_pinned_download_matches_size_sha256')
        denied(lambda:fixtures.write(clients['UploadA'],'put_object',Bucket=bucket,Key=clean['Key'],Body=b'changed',IfNoneMatch='*'),'PreconditionFailed');passed('conditional_write_prevents_replacement')
        denied(lambda:fixtures.write(clients['UploadA'],'put_object',Bucket=bucket,Key=clean['Key'],Body=b'changed'));passed('unconditional_overwrite_denied')
        denied(lambda:fixtures.write(clients['UploadA'],'put_object',Bucket=bucket,Key='quarantine/tenant-a/'+run_id+'/forged-upload',Body=data,IfNoneMatch='*',Tagging=SCAN_TAG+'=NO_THREATS_FOUND'));passed('tagged_upload_cannot_forge_clean')
        denied(lambda:fixtures.write(clients['UploadA'],'copy_object',Bucket=bucket,Key='quarantine/tenant-a/'+run_id+'/forged-copy',CopySource={'Bucket':bucket,**clean},TaggingDirective='REPLACE',Tagging=SCAN_TAG+'=NO_THREATS_FOUND'));passed('copy_cannot_replace_scan_tag')
        denied(lambda:clients['UploadA'].put_object_tagging(Bucket=bucket,**clean,Tagging={'TagSet':[{'Key':SCAN_TAG,'Value':'NO_THREATS_FOUND'}]}));passed('version_scan_tag_cannot_be_replaced')
        denied(lambda:clients['UploadA'].delete_object_tagging(Bucket=bucket,**clean));passed('version_scan_tag_cannot_be_deleted')
        require_same_bytes(get(clients['ReadA'],clean),identity);passed('original_scanned_version_stays_unchanged')
        denied(lambda:get(clients['UploadA'],clean));passed('uploader_has_no_read_privilege')
        denied(lambda:clients['ReadA'].get_object(Bucket=bucket,Key=clean['Key']));passed('reader_cannot_read_latest_without_version')
        other=put(clients['UploadB'],'quarantine/tenant-b/'+run_id+'/clean',data)
        require(scan_ready(wait_scan(other)),'tenant_b_not_clean')
        denied(lambda:get(clients['ReadA'],other));passed('tenant_a_reader_denied_tenant_b')
        denied(lambda:fixtures.write(clients['UploadA'],'put_object',Bucket=bucket,Key='quarantine/tenant-b/'+run_id+'/foreign',Body=data,IfNoneMatch='*'));passed('tenant_a_uploader_denied_tenant_b')
        anonymous=boto3.client('s3',region_name=REGION,config=Config(signature_version=UNSIGNED,connect_timeout=3,read_timeout=10,retries={'total_max_attempts':1}))
        denied(lambda:get(anonymous,clean));passed('anonymous_clean_version_denied')
        denied(lambda:clients['ReadA'].delete_object(Bucket=bucket,**clean));passed('reader_cannot_delete_version')
        # Standard harmless antivirus wiring test, assembled in memory; never saved to disk.
        eicar=b'X5O!P%@AP[4\\PZX54(P^)7CC)7}$'+b'EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*'
        threat=put(clients['UploadA'],'quarantine/tenant-a/'+run_id+'/antivirus-test',eicar)
        tags=wait_scan(threat)
        require(any(t['Key']==SCAN_TAG and t['Value']=='THREATS_FOUND' for t in tags),'antivirus_test_not_detected')
        passed('managed_scanner_detects_standard_antivirus_test')
        denied(lambda:get(clients['ReadA'],threat));passed('detected_test_version_stays_quarantined')
        result=dict(passed=True,checks=checks,check_count=len(checks),region=REGION,provider='AWS GuardDuty Malware Protection for S3',
                    engine_signature_version='provider_managed_not_exposed',scanner_plan_active=True,scan_records=scan_records,
                    setup_template_sha256=template_hash(),actual_file_authorization='deferred_FILE01_FILE02',parser_runtime='separate_probe_required')
    finally:
        cleanup=fixtures.cleanup()
        if result is not None:
            result['owned_fixture_cleanup']=cleanup
            result['passed']=result['passed'] and cleanup=='completed'
            output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        require(cleanup=='completed','owned_fixture_cleanup_'+cleanup)
    require(result is not None and result['passed'],'probe_failed')
    print(json.dumps(dict(passed=True,check_count=len(checks),region=REGION,owned_fixture_cleanup='completed',resources_deleted=False)))


def failed_output(args,fields):
    if args.command!='run':return
    try:
        prior=json.loads(args.output.read_text(encoding='utf-8'))
        prior.update(passed=False,status='failed',**fields)
        args.output.write_text(json.dumps(prior,indent=2)+'\n',encoding='utf-8')
    except Exception:pass


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    prep=sub.add_parser('prepare');prep.add_argument('--operator-principal-arn',required=True);prep.add_argument('--output',type=Path,default=ROOT/'local/spike03/setup.json')
    live=sub.add_parser('run');live.add_argument('--live',action='store_true');live.add_argument('--setup',type=Path,default=ROOT/'local/spike03/setup.json');live.add_argument('--output',type=Path,default=ROOT/'local/spike03/cloud-proof.json')
    args=parser.parse_args()
    try:
        if args.command=='prepare':prepare(args.operator_principal_arn,args.output)
        else:
            require(args.live,'explicit_live_required');args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text('{"passed":false,"status":"running"}\n',encoding='utf-8')
            run(boto3.Session(region_name=REGION),json.loads(args.setup.read_text(encoding='utf-8')),args.output)
    except ClientError as error:
        operation=getattr(error,'operation_name','unknown')
        code=error.response.get('Error',{}).get('Code','unknown')
        safe=lambda value:value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',value) else 'unknown'
        fields=dict(code='aws_operation_rejected',operation=safe(operation),provider_code=safe(code))
        failed_output(args,fields)
        print(json.dumps(dict(passed=False,**fields)),file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as error:
        fields=dict(code=str(error) if isinstance(error,ProbeRejected) else 'sanitized_operation_failure')
        failed_output(args,fields)
        print(json.dumps(dict(passed=False,**fields)),file=sys.stderr)
        raise SystemExit(1) from None


if __name__=='__main__':main()
