import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from botocore.exceptions import ReadTimeoutError
import pytest

HERE=Path(__file__).resolve().parents[3]/'scripts/spikes/storage'
ROOT=HERE.parents[2]


def module(name):
    spec=importlib.util.spec_from_file_location(name,HERE/(name+'.py'))
    result=importlib.util.module_from_spec(spec);sys.modules[name]=result;spec.loader.exec_module(result)
    return result


policy=module('file_policy')
cloud=module('cloud_probe')
parser_probe=module('parser_probe')


@pytest.mark.parametrize('size,accepted',[(0,False),(1,True),(policy.MAX_BYTES,True),(policy.MAX_BYTES+1,False)])
def test_actual_byte_limit_ignores_claimed_header(size,accepted):
    data=b'a'*size
    if accepted:
        actual=policy.read_bounded(io.BytesIO(data))
        assert policy.inspect_bytes(actual,'knowledge','text/plain').size==size
    else:
        with pytest.raises(policy.FileRejected):policy.read_bounded(io.BytesIO(data))


@pytest.mark.parametrize('mime,data,purpose',[
    ('image/png',b'not an image','receipt'),('image/jpeg',b'\x89PNG\r\n\x1a\n'+'x'.encode(),'receipt'),
    ('text/plain',b'%PDF-1.4\n','knowledge'),('application/pdf',b'wrong','knowledge'),
    ('text/plain',b'\x00binary','knowledge'),('text/plain',b'\xff','knowledge'),
    ('text/plain',b'a','receipt'),('image/png',b'\x89PNG\r\n\x1a\n','knowledge')])
def test_mime_purpose_and_binary_mismatch_fail_closed(mime,data,purpose):
    with pytest.raises(policy.FileRejected):policy.inspect_bytes(data,purpose,mime)


@pytest.mark.parametrize('status',[None,'THREATS_FOUND','UNSUPPORTED','ACCESS_DENIED','FAILED','PENDING','clean',True])
def test_only_provider_clean_status_unblocks_candidate(status):
    assert not policy.scan_ready([] if status is None else [{'Key':policy.SCAN_TAG,'Value':status}])


def test_duplicate_or_missing_scan_tag_never_unlocks():
    clean={'Key':policy.SCAN_TAG,'Value':'NO_THREATS_FOUND'}
    assert policy.scan_ready([clean])
    assert not policy.scan_ready([clean,clean])
    assert not policy.scan_ready({'status':'clean'})


def test_digest_and_actual_size_both_bind_the_bytes():
    identity=policy.inspect_bytes(b'fictional text','knowledge','text/plain')
    policy.require_same_bytes(b'fictional text',identity)
    for changed in (b'fictional TEXT',b'fictional text '):
        with pytest.raises(policy.FileRejected):policy.require_same_bytes(changed,identity)


@pytest.mark.parametrize('count',[0,11,True,1.0,'10'])
def test_receipt_count_rejects_unbounded_or_nonstrict_values(count):
    with pytest.raises(policy.FileRejected):policy.receipt_count(count)


def test_receipt_exact_count_ten_accepted():policy.receipt_count(10)


def test_setup_is_local_only_and_does_not_replace_existing_record(tmp_path,monkeypatch):
    monkeypatch.setattr(cloud.boto3,'Session',lambda *a,**k:pytest.fail('AWS session not permitted in prepare'))
    output=tmp_path/'setup.json'
    cloud.prepare('arn:aws:iam::111122223333:user/fictional-operator',output)
    data=json.loads(output.read_text())
    assert data['region']=='ap-southeast-1' and data['template_sha256']==cloud.template_hash()
    before=output.read_bytes()
    with pytest.raises(cloud.ProbeRejected):cloud.prepare(data['operator'],output)
    assert output.read_bytes()==before


def test_iam_roles_and_bucket_policy_enforce_trust_boundaries():
    template=json.loads((ROOT/'infra/aws/storage_spike.json').read_text(encoding='utf-8'))
    resources=template['Resources']
    assert len(resources)==8
    bucket=resources['Bucket']['Properties']
    assert all(bucket['PublicAccessBlockConfiguration'].values())
    assert bucket['VersioningConfiguration']['Status']=='Enabled'
    assert bucket['OwnershipControls']['Rules']==[{'ObjectOwnership':'BucketOwnerEnforced'}]
    assert not any(r['Type'] in {'AWS::GuardDuty::Detector','AWS::EC2::Instance','AWS::Lambda::Function'} for r in resources.values())
    for tenant in ('A','B'):
        upload=resources['Upload'+tenant+'Role']['Properties']['Policies'][0]['PolicyDocument']['Statement']
        assert [x for s in upload for x in s['Action']]==['s3:PutObject']
        assert all('tenant-'+tenant.lower()+'/' in r['Fn::Sub'] for s in upload for r in s['Resource'])
        read=resources['Read'+tenant+'Role']['Properties']['Policies'][0]['PolicyDocument']['Statement']
        actions=[x for s in read for x in s['Action']]
        assert 's3:GetObject' not in actions and 's3:PutObjectTagging' not in actions
        assert read[0]['Condition']['StringEquals']['s3:ExistingObjectTag/GuardDutyMalwareScanStatus']=='NO_THREATS_FOUND'
    statements=resources['BucketPolicy']['Properties']['PolicyDocument']['Statement']
    assert all(s['Effect']=='Deny' for s in statements)
    forge=next(s for s in statements if s['Sid']=='ProviderOwnsScanTags')
    assert forge['Condition']['ArnNotEquals']['aws:PrincipalArn']=={'Fn::GetAtt':['ScanRole','Arn']}
    assert 's3:DeleteObjectVersionTagging' in forge['Action']


def test_parser_container_has_no_mount_or_credential_forwarding():
    options=parser_probe.options('eai-parser-'+'a'*32)
    for key,value in (('--network','none'),('--memory','512m'),('--memory-swap','512m'),('--cpus','1'),('--pids-limit','32'),('--user','65534:65534')):
        assert options[options.index(key)+1]==value
    assert '--read-only' in options and '--cap-drop' in options
    assert not set(options)&{'-v','--volume','--mount','-e','--env','--env-file','--privileged'}
    dockerignore=(HERE/'.dockerignore').read_text().splitlines()
    assert dockerignore[0]=='*' and not any('.env' in line or '.aws' in line for line in dockerignore)


def test_cloudformation_output_names_resolve_probe_role_contract():
    template=json.loads(cloud.TEMPLATE.read_text(encoding='utf-8'))
    outputs=template['Outputs']
    for name in ('UploadA','ReadA','UploadB','ReadB','Scan'):
        assert outputs[name+'RoleArn']['Value']=={'Fn::GetAtt':[name+'Role','Arn']}
    mapping={'Bucket.Arn':'arn:aws:s3:::fictional-bucket'}
    scopes=template['Resources']['ReadARole']['Properties']['Policies'][0]['PolicyDocument']['Statement'][0]['Resource']
    assert cloud.resolve(scopes,mapping)==['arn:aws:s3:::fictional-bucket/quarantine/tenant-a/*','arn:aws:s3:::fictional-bucket/unscanned/tenant-a/*']


class FakeS3:
    def __init__(self):self.versions={};self.deleted=[];self.lost_reply=False
    def put_object(self,**values):
        key=values['Key'];version=str(len(self.versions.get(key,[]))+1)
        self.versions.setdefault(key,[]).append(version)
        if self.lost_reply:raise ReadTimeoutError(endpoint_url='https://fictional.invalid')
        return {'VersionId':version}
    def list_object_versions(self,**values):
        return dict(Versions=[{'Key':key,'VersionId':v} for key,versions in self.versions.items() if key.startswith(values['Prefix']) for v in versions],IsTruncated=False)
    def delete_object(self,**values):
        self.versions[values['Key']].remove(values['VersionId']);self.deleted.append((values['Key'],values['VersionId']))


def test_unexpected_negative_write_success_is_still_cleaned(tmp_path):
    owner=FakeS3();run='a'*32;key='quarantine/tenant-a/'+run+'/clean'
    fixture=cloud.CloudFixtures(owner,'fictional',run,tmp_path/'cleanup.json')
    fixture.write(owner,'put_object',Bucket='fictional',Key=key,Body=b'first')
    with pytest.raises(cloud.ProbeRejected):
        cloud.denied(lambda:fixture.write(owner,'put_object',Bucket='fictional',Key=key,Body=b'unexpected-overwrite'))
    owner.versions[key+'-foreign-suffix']=['foreign-version']
    assert fixture.cleanup()=='completed' and owner.versions[key]==[]
    assert owner.versions[key+'-foreign-suffix']==['foreign-version']
    assert len(owner.deleted)==2


def test_lost_write_reply_retains_intent_and_never_claims_cleanup_complete(tmp_path):
    owner=FakeS3();owner.lost_reply=True;run='b'*32;key='quarantine/tenant-b/'+run+'/unknown'
    manifest=tmp_path/'cleanup.json';fixture=cloud.CloudFixtures(owner,'fictional',run,manifest)
    with pytest.raises(ReadTimeoutError):fixture.write(owner,'put_object',Bucket='fictional',Key=key,Body=b'fictional')
    assert fixture.cleanup()=='unresolved' and owner.versions[key]==[]
    assert json.loads(manifest.read_text())['keys']==[key]


def test_cleanup_write_scope_is_exact_and_registered_before_attempt(tmp_path):
    fixture=cloud.CloudFixtures(FakeS3(),'fictional','a'*32,tmp_path/'cleanup.json')
    with pytest.raises(cloud.ProbeRejected):
        fixture.write(FakeS3(),'put_object',Bucket='foreign',Key='quarantine/tenant-a/'+'a'*32+'/clean',Body=b'data')
    assert fixture.keys==set()


def test_failed_cloud_proof_does_not_remain_running(tmp_path):
    path=tmp_path/'proof.json';path.write_text('{"passed":false,"status":"running"}')
    cloud.failed_output(SimpleNamespace(command='run',output=path),{'code':'scanner_deadline'})
    assert json.loads(path.read_text())=={'passed':False,'status':'failed','code':'scanner_deadline'}


def test_no_implicit_sdk_write_retries_hide_ambiguous_first_attempt():
    assert cloud.SDK.retries['total_max_attempts']==1


def test_retried_success_response_cannot_certify_cleanup(tmp_path):
    class RetriedS3(FakeS3):
        def put_object(self,**values):
            return {**super().put_object(**values),'ResponseMetadata':{'RetryAttempts':1}}
    owner=RetriedS3();run='c'*32;fixture=cloud.CloudFixtures(owner,'fictional',run,tmp_path/'cleanup.json')
    fixture.write(owner,'put_object',Bucket='fictional',Key='quarantine/tenant-a/'+run+'/retried',Body=b'fictional')
    assert fixture.ambiguous and fixture.cleanup()=='unresolved'
