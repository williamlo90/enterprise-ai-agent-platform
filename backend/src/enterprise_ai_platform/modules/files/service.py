import asyncio
import hashlib
import time
import math
from uuid import uuid4
from enterprise_ai_platform.modules.files.domain import FileFailure,Upload,public_file,uuid_text,MAX_FILE_BYTES
from enterprise_ai_platform.modules.files.ports import FileRepository,PrivateStorage,FileParser
from enterprise_ai_platform.modules.identity.service import digest,opaque
from enterprise_ai_platform.modules.jobs.service import JobService
from enterprise_ai_platform.modules.jobs.ports import Lease
from uuid import UUID
from enterprise_ai_platform.modules.receipts.ports import ReceiptFailure,ClaimAccess,ReceiptRef
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure


class FilesService:
    def __init__(self,repository:FileRepository,storage:PrivateStorage,origin:str,*,claim_authority=None,clock=time.time):
        self.repository,self.storage,self.origin=repository,storage,origin
        self.clock=clock
        self.claim_authority=claim_authority

    async def claim_content(self,session,file_id,claim_id):
        if self.claim_authority is None:raise FileFailure(503)
        uuid_text(file_id);uuid_text(claim_id);token=self.session_hash(session)
        async def call(operation,payload):
            result=await self.repository.claim_call(token,operation,payload)
            expiry=result.get('authority_expires_at')
            if type(expiry) not in (int,float) or not math.isfinite(expiry) or self.clock()>=expiry:raise FileFailure(401)
            return result
        try:
            context=(await call('context',{}))['context']
            original={k:context[k] for k in ('tenant_id','actor_id','membership_id','session_id','access_revision')}
            async def authority():
                access,envelope=await self.claim_authority.authorize(dict(context),claim_id)
                if type(access) is not ClaimAccess or (access.tenant_id,access.actor_id,access.claim_id)!=(original['tenant_id'],original['actor_id'],claim_id):raise FileFailure(503)
                return access,envelope
            access,envelope=await authority()
            row=await call('reference',dict(id=file_id,claim_id=claim_id,attestation=envelope))
            if row['id']!=file_id or row['purpose']!='receipt' or row['scan_state']!='ready' or not access.permits(ReceiptRef(row['id'],row['version'],row['digest']),tenant_id=original['tenant_id'],actor_id=original['actor_id'],claim_id=claim_id,now=self.clock()):raise FileFailure(404)
            data=await self.storage.read(row)
            if len(data)!=row['size_bytes'] or len(data)>MAX_FILE_BYTES or hashlib.sha256(data).hexdigest()!=row['digest']:raise FileFailure(503)
            current,final_envelope=await authority()
            # Renewed expiry is permitted; source revision/version/material must
            # remain the same throughout the bounded storage read.
            first=access.wire();last=current.wire();first.pop('expires_at');last.pop('expires_at')
            if first!=last:raise FileFailure(409)
            final=await call('download',dict(id=file_id,claim_id=claim_id,attestation=final_envelope))
            if {k:v for k,v in final.items() if k!='authority_expires_at'}!={k:v for k,v in row.items() if k!='authority_expires_at'}:raise FileFailure(409)
            if self.clock()>=current.expires_at:raise FileFailure(401)
            return {**public_file(final),'_receipt_authority_expires_at':int(min(current.expires_at,final['authority_expires_at']))},data
        except ReceiptFailure as error:raise FileFailure(error.status) from None
        except BrowserContextFailure:raise
        except FileFailure:raise
        except Exception:raise FileFailure(503) from None

    async def call(self,token,operation,payload):
        row=await self.repository.call(token,operation,payload)
        deadline=row.get('authority_expires_at')
        if isinstance(deadline,bool) or not isinstance(deadline,(int,float)) or not math.isfinite(deadline) or self.clock()>=deadline:raise FileFailure(401)
        return row

    def session_hash(self,session):
        if not opaque(session):raise FileFailure(401)
        return digest('session',session)

    def guard(self,headers,mime):
        if headers.get('origin')!=self.origin or headers.get('content-type')!=mime or not opaque(headers.get('x-csrf-token')):
            raise FileFailure(403)
        return digest('csrf',headers['x-csrf-token'])

    async def bind(self,token,row,object_version,csrf):
        try:
            return await self.call(token,'bind',dict(id=row['id'],version=row['version'],object_version=object_version,csrf_hash=csrf))
        except FileFailure as error:
            if error.status!=409:raise
            # Concurrent retry may have completed the exact bind while this request
            # waited. Reauthorize, then accept only the same immutable byte identity.
            current=await self.call(token,'status',dict(id=row['id']))
            if current['object_version']!=object_version or any(current[k]!=row[k] for k in ('digest','size_bytes','mime','purpose')):raise FileFailure(409)
            return current

    async def upload(self,session,headers,upload):
        token=self.session_hash(session)
        csrf=self.guard(headers,upload.mime)
        values=dict(id=str(uuid4()),purpose=upload.purpose,mime=upload.mime,size_bytes=len(upload.data),digest=upload.digest,
                    idempotency_key=upload.key,collection_id=upload.collection_id,csrf_hash=csrf)
        row=await self.call(token,'reserve',values)
        if row['object_version'] is not None:return await self.upload_result(token,row)
        object_version=await self.storage.recover(row) if row['replayed'] else None
        if object_version is None:
            try:await self.call(token,'upload_attempt',dict(id=row['id'],version=row['version'],csrf_hash=csrf))
            except FileFailure as error:
                if error.status==409:
                    current=await self.call(token,'status',dict(id=row['id']))
                    if current['object_version'] is not None:return await self.upload_result(token,current)
                raise
            # Retry only exact bytes/key with If-None-Match; never a new object key.
            # Durable budget bounds attempts, including crashes and lost replies.
            try:object_version=await self.storage.upload(row['object_key'],upload.data,upload.digest)
            except FileFailure as error:
                if error.status!=409:raise
                object_version=await self.storage.recover(row)
                if object_version is None:return await self.upload_result(token,row)
        bound=await self.bind(token,row,object_version,csrf)
        return await self.upload_result(token,bound)

    async def upload_result(self,token,row):
        # Every successful admission/replay branch ends at the same fresh,
        # committed status read, including after a storage await or bind race.
        final=await self.call(token,'status',dict(id=row['id']))
        if any(final.get(key)!=row.get(key) for key in ('id','purpose','mime','size_bytes','digest','collection_id')):raise FileFailure(409)
        return public_file(final)

    async def status(self,session,file_id):
        uuid_text(file_id)
        return public_file(await self.call(self.session_hash(session),'status',dict(id=file_id)))

    async def content(self,session,file_id):
        uuid_text(file_id);token=self.session_hash(session)
        row=await self.call(token,'reference',dict(id=file_id))
        if row['scan_state']!='ready':raise FileFailure(404)
        data=await self.storage.read(row)
        if len(data)!=row['size_bytes'] or len(data)>MAX_FILE_BYTES or hashlib.sha256(data).hexdigest()!=row['digest']:raise FileFailure(503)
        # Buffer bounded bytes; no output is released before a fresh current-access check.
        final=await self.call(token,'download',dict(id=file_id,version=row['version']))
        if {k:v for k,v in final.items() if k!='authority_expires_at'}!={k:v for k,v in row.items() if k!='authority_expires_at'}:raise FileFailure(409)
        return public_file(row),data


class FileInspection:
    """Explicit worker handler; job credential is never a browser/session authority."""
    def __init__(self,repository,storage,parser,job_service:JobService,credential_hash):
        self.repository,self.storage,self.parser=repository,storage,parser
        self.jobs,self.credential_hash=job_service,credential_hash

    async def process(self,claim):
        if claim.get('job_type')!='file.inspect':raise FileFailure(400)
        lease=Lease(UUID(claim['id']),UUID(claim['tenant_id']),claim['generation'])
        payload=dict(credential_hash=self.credential_hash,job_id=str(lease.id),generation=lease.generation)
        async def heartbeat():
            while True:
                await asyncio.sleep(4)
                await self.jobs.heartbeat(lease)
        async def inspect():
            row=await self.repository.call(None,'work',payload)
            scan=await self.storage.scan(row)
            if scan=='pending':
                renewal.cancel()
                await asyncio.gather(renewal,return_exceptions=True)
                await self.jobs.fail(lease,'rate_limited')
                state=await self.jobs.inspect(lease.id)
                return {'state':state['state']}
            if scan!='clean':accepted=False
            else:
                data=await self.storage.read(row)
                if len(data)!=row['size_bytes'] or hashlib.sha256(data).hexdigest()!=row['digest']:raise FileFailure(503)
                accepted=await self.parser.inspect(data,row['purpose'],row['mime'])
            renewal.cancel()
            await asyncio.gather(renewal,return_exceptions=True)
            return await self.repository.call(None,'processed',{**payload,'accepted':accepted})
        async with asyncio.timeout(100):
            async with asyncio.TaskGroup() as group:
                renewal=group.create_task(heartbeat())
                try:return await inspect()
                finally:renewal.cancel()
