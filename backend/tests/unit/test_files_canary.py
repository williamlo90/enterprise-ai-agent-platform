import asyncio
import json
from pathlib import Path
import pytest
from botocore.exceptions import ReadTimeoutError,ClientError
from enterprise_ai_platform import file_cloud_check as canary
from enterprise_ai_platform.config import Settings,ConfigurationError


def test_canary_resources_keep_backend_bytes_private_and_separate_from_identity():
    template=json.loads((Path(__file__).resolve().parents[3]/'infra/aws/files_canary.json').read_text(encoding='utf-8'))
    resources=template['Resources'];assert len(resources)==5
    assert set(resources)=={'Bucket','BucketPolicy','FileRole','ScanRole','MalwarePlan'}
    assert resources['Bucket']['DeletionPolicy']=='Retain'
    assert all(resources['Bucket']['Properties']['PublicAccessBlockConfiguration'].values())
    assert not any(r['Type'].startswith(('AWS::Lambda','AWS::Cognito','AWS::EC2')) for r in resources.values())
    statements=resources['FileRole']['Properties']['Policies'][0]['PolicyDocument']['Statement']
    permissions={action for statement in statements for action in statement['Action']}
    assert 's3:GetObjectVersionAttributes' in permissions and 's3:GetObjectVersion' in permissions
    assert not permissions & {'s3:GetObject','s3:DeleteObject','s3:PutObjectTagging','s3:PutObjectVersionTagging'}
    reads=next(s for s in statements if 's3:GetObjectVersionAttributes' in s['Action'])
    assert reads['Condition']['StringEquals']['s3:ExistingObjectTag/GuardDutyMalwareScanStatus']=='NO_THREATS_FOUND'


class Owner:
    def __init__(self):self.versions={};self.deleted=[]
    def put_object(self,**values):
        self.versions[values['Key']]='fictional-version'
        raise ReadTimeoutError(endpoint_url='https://fictional.invalid')
    def list_object_versions(self,**values):
        return dict(Versions=[dict(Key=k,VersionId=v) for k,v in self.versions.items() if k.startswith(values['Prefix'])],IsTruncated=False)
    def delete_object(self,**values):
        del self.versions[values['Key']];self.deleted.append(values['Key'])


def test_cloud_canary_lost_reply_keeps_exact_intent_and_cleanup_unresolved(tmp_path):
    owner=Owner();recorder=canary.Recorder(owner,owner,'fictional-private',tmp_path/'intents.json');tenant='a'*8+'-aaaa-aaaa-aaaa-'+'a'*12
    recorder.tenants={tenant};key='quarantine/'+tenant+'/'+'b'*8+'-bbbb-bbbb-bbbb-'+'b'*12
    with pytest.raises(ReadTimeoutError):recorder.put_object(Bucket='fictional-private',Key=key,Body=b'fictional')
    assert json.loads((tmp_path/'intents.json').read_text())['keys']==[key]
    owner.versions[key+'-outside']='outside-version'
    assert recorder.cleanup()=='unresolved' and owner.deleted==[key] and key+'-outside' in owner.versions


def test_new_recorder_cannot_replace_existing_unresolved_intents(tmp_path):
    path=tmp_path/'intents.json';path.write_text('{"keys":["existing"],"cleanup_status":"unresolved"}')
    before=path.read_bytes()
    with pytest.raises(FileExistsError):canary.Recorder(Owner(),Owner(),'fictional-private',path)
    assert path.read_bytes()==before


@pytest.mark.parametrize('host',['AWS_LAMBDA_FUNCTION_NAME','VERCEL'])
def test_file_upload_flag_rejects_incompatible_buffered_host(host,monkeypatch):
    monkeypatch.setenv(host,'1')
    with pytest.raises(ValueError,match='File upload transport'):Settings(env='test',host='127.0.0.1',port=8000,files_enabled=True)


def test_recovery_diagnostics_keep_provider_code_without_private_values(tmp_path):
    class Provider:
        def get_object_attributes(self,**values):
            raise ClientError({'Error':{'Code':'AccessDenied','Message':'private-message'},
                'ResponseMetadata':{'RequestId':'private-request'}},'GetObjectAttributes')
    recorder=canary.Recorder(Provider(),Owner(),'fictional-private',tmp_path/'intents.json')
    with pytest.raises(ClientError):
        recorder.get_object_attributes(Bucket='private-bucket',Key='private-key',VersionId='private-version')
    assert recorder.diagnostics==[{'operation':'get_object_attributes','outcome':'provider_error','provider_code':'AccessDenied'}]
    assert 'private' not in json.dumps(recorder.diagnostics)


def test_recovery_diagnostics_record_missing_checksum_without_recording_bytes(tmp_path):
    class Provider:
        def get_object_attributes(self,**values):return {'ObjectSize':9,'Checksum':{'ChecksumCRC64NVME':'private-checksum'}}
    recorder=canary.Recorder(Provider(),Owner(),'fictional-private',tmp_path/'intents.json')
    result=recorder.get_object_attributes(Bucket='private-bucket')
    assert result['ObjectSize']==9
    assert recorder.diagnostics==[{'operation':'get_object_attributes','outcome':'ok','size_present':True,'sha256_present':False}]
    assert 'private' not in json.dumps(recorder.diagnostics)


@pytest.mark.parametrize('status',[None,409,503])
def test_recovery_storage_diagnostic_identifies_failure_before_bind(status):
    class Adapter:
        async def recover(self,row):
            if status is not None:raise canary.FileFailure(status)
            return 'private-version'
    store=canary.Store(Adapter())
    if status is None:
        assert asyncio.run(store.recover({}))=='private-version'
        assert store.recovery_storage=={'outcome':'found'}
    else:
        with pytest.raises(canary.FileFailure):asyncio.run(store.recover({}))
        assert store.recovery_storage=={'outcome':'failed','http_status':status}
    assert 'private' not in json.dumps(store.recovery_storage)
