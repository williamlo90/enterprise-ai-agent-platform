import asyncio
import base64
from contextlib import closing
from dataclasses import replace
import hashlib
import io
import json
from uuid import uuid4
import boto3
from botocore.config import Config
from botocore.response import StreamingBody
from botocore.stub import Stubber
import pytest
import httpx
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.composition import ApplicationServices
from enterprise_ai_platform.modules.files.domain import Upload,FileFailure,MAX_FILE_BYTES
from enterprise_ai_platform.modules.files.service import FilesService,FileInspection
from enterprise_ai_platform.infrastructure.storage.s3 import S3Files,S3_CONFIG
from enterprise_ai_platform.api.boundary import RequestBoundary


def upload(**kwargs):
    return Upload(**{'purpose':'knowledge','mime':'text/plain','data':b'fictional','key':str(uuid4()),**kwargs})


@pytest.mark.parametrize('field,value,status',[('purpose','arbitrary',400),('mime','text/html',400),('key','not-uuid',400),
    ('collection_id','bad',400),('data',b'',413),('data',b'x'*(MAX_FILE_BYTES+1),413),('data',b'\xff',422),
    ('data',b'\x00binary',422),('data',b'%PDF-1.4',422),('purpose','receipt',422)])
def test_upload_strict_preflight(field,value,status):
    with pytest.raises(FileFailure) as error:upload(**{field:value})
    assert error.value.status==status


def test_exact_file_limit_preflight_is_not_api_default_limit():
    value=upload(data=b'a'*MAX_FILE_BYTES)
    assert value.digest==hashlib.sha256(value.data).hexdigest()
    assert 'fictional' not in repr(value)


def client():
    return boto3.client('s3',region_name='ap-southeast-1',aws_access_key_id='fictional',aws_secret_access_key='fictional',config=S3_CONFIG)


def reference():
    return dict(object_key='quarantine/'+str(uuid4())+'/'+str(uuid4()),object_version='fictional-version',digest=hashlib.sha256(b'fictional').hexdigest(),size_bytes=9)


def run_adapter(method,rows):
    s3=client();adapter=S3Files(s3,'fictional-private-bucket')
    async def execute():
        try:return await method(adapter)
        finally:await adapter.close()
    with Stubber(s3) as stub:
        for operation,response,parameters in rows:stub.add_response(operation,response,parameters)
        result=asyncio.run(execute());stub.assert_no_pending_responses()
    return result


def test_storage_download_uses_exact_version_and_integrity():
    row=reference()
    result=run_adapter(lambda a:a.read(row),[('get_object',{'Body':StreamingBody(io.BytesIO(b'fictional'),9),'ContentLength':9},
        {'Bucket':'fictional-private-bucket','Key':row['object_key'],'VersionId':row['object_version'],'ChecksumMode':'ENABLED'})])
    assert result==b'fictional'


def test_storage_digest_mismatch_never_returns_bytes():
    row=reference()
    with pytest.raises(FileFailure):
        run_adapter(lambda a:a.read(row),[('get_object',{'Body':StreamingBody(io.BytesIO(b'different'),9),'ContentLength':9},
            {'Bucket':'fictional-private-bucket','Key':row['object_key'],'VersionId':row['object_version'],'ChecksumMode':'ENABLED'})])


@pytest.mark.parametrize('tag,expected',[('NO_THREATS_FOUND','clean'),('THREATS_FOUND','rejected'),(None,'pending')])
def test_scanner_verdict_exact_version(tag,expected):
    row=reference()
    tags=[] if tag is None else [{'Key':'GuardDutyMalwareScanStatus','Value':tag}]
    result=run_adapter(lambda a:a.scan(row),[('get_object_tagging',{'TagSet':tags},{'Bucket':'fictional-private-bucket','Key':row['object_key'],'VersionId':row['object_version']})])
    assert result==expected


@pytest.mark.parametrize('tag',['UNSUPPORTED','ACCESS_DENIED','FAILED','unknown'])
def test_nonclean_provider_error_is_unavailable_not_safe(tag):
    row=reference()
    with pytest.raises(FileFailure):
        run_adapter(lambda a:a.scan(row),[('get_object_tagging',{'TagSet':[{'Key':'GuardDutyMalwareScanStatus','Value':tag}]},
            {'Bucket':'fictional-private-bucket','Key':row['object_key'],'VersionId':row['object_version']})])


@pytest.mark.parametrize('tags',[[],[{'Key':'GuardDutyMalwareScanStatus','Value':'THREATS_FOUND'}]])
def test_reconcile_does_not_read_bytes_before_clean(tags):
    row=reference()
    assert run_adapter(lambda a:a.recover(row),[
        ('list_object_versions',{'Versions':[{'Key':row['object_key'],'VersionId':'version'}],'IsTruncated':False}, {'Bucket':'fictional-private-bucket','Prefix':row['object_key'],'MaxKeys':10}),
        ('get_object_tagging',{'TagSet':tags},{'Bucket':'fictional-private-bucket','Key':row['object_key'],'VersionId':'version'})]) is None


def test_reconcile_absence_never_writes_or_deletes():
    row=reference()
    assert run_adapter(lambda a:a.recover(row),[('list_object_versions',{'Versions':[],'IsTruncated':False},
        {'Bucket':'fictional-private-bucket','Prefix':row['object_key'],'MaxKeys':10})]) is None


def test_reconcile_clean_exact_version_verifies_actual_bytes_and_size():
    row=reference()
    assert run_adapter(lambda a:a.recover(row),[
        ('list_object_versions',{'Versions':[{'Key':row['object_key'],'VersionId':'version'}],'IsTruncated':False},{'Bucket':'fictional-private-bucket','Prefix':row['object_key'],'MaxKeys':10}),
        ('get_object_tagging',{'TagSet':[{'Key':'GuardDutyMalwareScanStatus','Value':'NO_THREATS_FOUND'}]},{'Bucket':'fictional-private-bucket','Key':row['object_key'],'VersionId':'version'}),
        ('get_object',{'Body':StreamingBody(io.BytesIO(b'fictional'),9),'ContentLength':9},
            {'Bucket':'fictional-private-bucket','Key':row['object_key'],'VersionId':'version','ChecksumMode':'ENABLED'})])=='version'


class Repository:
    def __init__(self,row):self.row=row;self.calls=[];self.denied=False
    async def call(self,token,operation,payload):
        self.calls.append((token,operation,payload))
        if self.denied:raise FileFailure(404)
        return dict(self.row)


def test_download_checks_current_authority_after_provider_await():
    row={'id':str(uuid4()),'purpose':'knowledge','mime':'text/plain','size_bytes':9,'scan_state':'ready','version':3,'authority_expires_at':9999999999,**reference()}
    repo=Repository(row)
    class Storage:
        async def read(self,row):repo.denied=True;return b'fictional'
    with pytest.raises(FileFailure) as error:asyncio.run(FilesService(repo,Storage(),'https://files.example.test').content('a'*43,row['id']))
    assert error.value.status==404 and [c[1] for c in repo.calls]==['reference','download']
    assert all(c[0]!='a'*43 for c in repo.calls)


def test_expiry_during_last_database_await_rejects_buffered_content():
    row={'id':str(uuid4()),'purpose':'knowledge','mime':'text/plain','size_bytes':9,'scan_state':'ready','version':3,'authority_expires_at':200,**reference()}
    class LateRepository(Repository):
        async def call(self,token,operation,payload):
            value=await super().call(token,operation,payload)
            if operation=='download':value['authority_expires_at']=100
            return value
    class Storage:
        async def read(self,row):return b'fictional'
    with pytest.raises(FileFailure) as error:
        asyncio.run(FilesService(LateRepository(row),Storage(),'https://files.example.test',clock=lambda:100).content('a'*43,row['id']))
    assert error.value.status==401


@pytest.mark.parametrize('origin,csrf',[('https://evil.example.test','a'*43),('https://files.example.test','missing'),('__duplicate__','a'*43)])
def test_file_csrf_guard_does_not_accept_arbitrary_mime_origin_headers(origin,csrf):
    service=FilesService(None,None,'https://files.example.test')
    with pytest.raises(FileFailure):service.guard({'origin':origin,'content-type':'text/plain','x-csrf-token':csrf},'text/plain')


@pytest.mark.parametrize('phase',['disconnect','timeout','oversize','duplicate_length'])
def test_file_boundary_actual_stream_failure_releases_slot(phase):
    async def check():
        reached=[];sent=[]
        async def app(scope,receive,send):reached.append(True)
        boundary=RequestBoundary(app,max_body_bytes=1,body_timeout=.02,file_body_bytes=3)
        headers=[(b'content-length',b'1'),(b'content-length',b'1')] if phase=='duplicate_length' else []
        scope={'type':'http','method':'POST','path':'/api/v1/files','headers':headers,'state':{}}
        async def receive():
            if phase=='disconnect':return {'type':'http.disconnect'}
            if phase=='timeout':await asyncio.sleep(1)
            return {'type':'http.request','body':b'1234','more_body':False}
        async def send(message):sent.append(message)
        await boundary(scope,receive,send)
        assert not reached and boundary.file_slots._value==2
        if phase!='disconnect':
            start=next(m for m in sent if m['type']=='http.response.start')
            assert start['status']=={'timeout':408,'oversize':413,'duplicate_length':400}[phase]
            assert (b'cache-control',b'no-store') in start['headers']
    asyncio.run(check())


@pytest.mark.parametrize('query',['purpose=knowledge&unexpected=','purpose=knowledge&purpose='])
def test_blank_query_values_cannot_hide_unknown_or_duplicate_fields(query):
    class Files:
        async def upload(self,*args):pytest.fail('Invalid query must not reach upload service.')
    async def check():
        app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=None,files=Files()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://files.example.test') as client:
            result=await client.post('/api/v1/files?'+query,content=b'fictional',headers={'content-type':'text/plain','idempotency-key':str(uuid4())})
            assert result.status_code==400 and result.headers['cache-control']=='no-store'
    asyncio.run(check())


def test_pending_scan_does_not_report_retry_after_budget_makes_job_dead():
    class Repo:
        async def call(self,*args):return {'id':str(uuid4())}
    class Storage:
        async def scan(self,row):return 'pending'
    class Jobs:
        async def fail(self,*args):return {'status':'ok'}
        async def inspect(self,*args):return {'state':'dead'}
    worker=FileInspection(Repo(),Storage(),None,Jobs(),'a'*64)
    result=asyncio.run(worker.process(dict(id=str(uuid4()),tenant_id=str(uuid4()),generation=5,job_type='file.inspect')))
    assert result=={'state':'dead'}


@pytest.mark.parametrize('data,size',[(b'different',9),(b'fictional',8),(b'fictiona',9)])
def test_reconcile_clean_version_never_binds_wrong_or_truncated_bytes(data,size):
    row=reference()
    with pytest.raises(FileFailure):
        run_adapter(lambda a:a.recover(row),[
            ('list_object_versions',{'Versions':[{'Key':row['object_key'],'VersionId':'version'}],'IsTruncated':False},
                {'Bucket':'fictional-private-bucket','Prefix':row['object_key'],'MaxKeys':10}),
            ('get_object_tagging',{'TagSet':[{'Key':'GuardDutyMalwareScanStatus','Value':'NO_THREATS_FOUND'}]},
                {'Bucket':'fictional-private-bucket','Key':row['object_key'],'VersionId':'version'}),
            ('get_object',{'Body':StreamingBody(io.BytesIO(data),size),'ContentLength':size},
                {'Bucket':'fictional-private-bucket','Key':row['object_key'],'VersionId':'version','ChecksumMode':'ENABLED'})])
