"""Version-pinned S3 bytes; callers never choose endpoint URLs or supply credentials."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import re
import time
from botocore.config import Config
from botocore.exceptions import ClientError
from enterprise_ai_platform.infrastructure.database.connection import finish_cleanup
from enterprise_ai_platform.modules.files.domain import FileFailure,MAX_FILE_BYTES

S3_CONFIG=Config(connect_timeout=3,read_timeout=5,retries={'total_max_attempts':1})


class S3Files:
    def __init__(self,client,bucket):
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',bucket):raise ValueError('Invalid storage configuration.')
        self.client,self.bucket=client,bucket
        self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='private-storage')
        self.slots=asyncio.Semaphore(2)

    async def call(self,function):
        async with self.slots:
            future=asyncio.get_running_loop().run_in_executor(self.pool,function)
            try:return await asyncio.shield(future)
            except asyncio.CancelledError as cancelled:
                # Keep the slot until bounded SDK work actually finishes; do not
                # accumulate detached upload threads after request cancellation.
                try:await finish_cleanup(asyncio.shield(future))
                except Exception:pass
                raise cancelled
            except FileFailure:raise
            except Exception:raise FileFailure(503) from None

    async def close(self):
        self.pool.shutdown(wait=False,cancel_futures=True)

    def key(self,value):
        if not isinstance(value,str) or not re.fullmatch(r'quarantine/[a-f0-9-]{36}/[a-f0-9-]{36}',value):raise FileFailure(503)
        return value

    def ref(self,row):
        key=self.key(row['object_key']);version=row.get('object_version')
        if not isinstance(version,str) or not 1<=len(version)<=1024 or version=='null':raise FileFailure(503)
        return dict(Bucket=self.bucket,Key=key,VersionId=version)

    async def upload(self,key,data,digest):
        self.key(key)
        if not 0<len(data)<=MAX_FILE_BYTES or hashlib.sha256(data).hexdigest()!=digest:raise FileFailure(503)
        def put():
            try:
                result=self.client.put_object(Bucket=self.bucket,Key=key,Body=data,IfNoneMatch='*',ServerSideEncryption='AES256',
                    ContentType='application/octet-stream',ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode())
            except ClientError as error:
                if error.response.get('Error',{}).get('Code')=='PreconditionFailed':raise FileFailure(409) from None
                raise
            version=result.get('VersionId')
            if result.get('ResponseMetadata',{}).get('RetryAttempts',0) or not isinstance(version,str) or not 1<=len(version)<=1024 or version=='null':raise FileFailure(503)
            return version
        return await self.call(put)

    async def recover(self,row):
        key=self.key(row['object_key'])
        def lookup():
            # Discover one exact version, then read only after provider-clean.
            # Recovery uses the same bounded, integrity-checked version read as
            # downloads; it does not require a separate attributes permission.
            page=self.client.list_object_versions(Bucket=self.bucket,Prefix=key,MaxKeys=10)
            exact=[v for v in page.get('Versions',[]) if v['Key']==key]
            if page.get('IsTruncated') or len(exact)>1 or any(v['Key']==key for v in page.get('DeleteMarkers',[])):raise FileFailure(503)
            if not exact:return None
            version=exact[0]['VersionId']
            tags=self.client.get_object_tagging(Bucket=self.bucket,Key=key,VersionId=version)['TagSet']
            if [t['Value'] for t in tags if t['Key']=='GuardDutyMalwareScanStatus']!=['NO_THREATS_FOUND']:return None
            return version
        version=await self.call(lookup)
        if version is None:return None
        # Never bind a discovered version on metadata alone. This checks actual
        # size and SHA256, retains clean-only IAM enforcement, and releases no
        # bytes to the caller. Binding still performs a fresh authority check.
        await self.read({**row,'object_version':version})
        return version

    async def scan(self,row):
        reference=self.ref(row)
        def tags():
            values=self.client.get_object_tagging(**reference)['TagSet']
            scans=[t['Value'] for t in values if t['Key']=='GuardDutyMalwareScanStatus']
            if not scans:return 'pending'
            if scans==['NO_THREATS_FOUND']:return 'clean'
            if scans==['THREATS_FOUND']:return 'rejected'
            raise FileFailure(503)  # unsupported/error/unknown is not a clean verdict.
        return await self.call(tags)

    async def read(self,row):
        reference=self.ref(row)
        def get():
            start=time.monotonic();response=self.client.get_object(**reference,ChecksumMode='ENABLED')
            with closing(response['Body']) as stream:
                if response.get('ContentLength')!=row['size_bytes']:raise FileFailure(503)
                data=bytearray()
                while True:
                    if time.monotonic()-start>15:raise FileFailure(503)
                    block=stream.read(min(65536,MAX_FILE_BYTES+1-len(data)))
                    if not block:break
                    data.extend(block)
                    if len(data)>MAX_FILE_BYTES:raise FileFailure(503)
            if len(data)!=row['size_bytes'] or hashlib.sha256(data).hexdigest()!=row['digest']:raise FileFailure(503)
            return bytes(data)
        return await self.call(get)
