"""Draft admission and fenced ingestion; publication is deliberately separate."""
import asyncio
from dataclasses import asdict
import hashlib
import math
import json
import time
from datetime import date
from uuid import UUID,uuid4
from enterprise_ai_platform.modules.knowledge.domain import KnowledgeFailure,identifier,chunks,MODEL,CHUNK_CONFIG
from enterprise_ai_platform.modules.knowledge.ports import KnowledgeRepository
from enterprise_ai_platform.modules.identity.service import digest,opaque
from enterprise_ai_platform.modules.jobs.ports import Lease
from enterprise_ai_platform.modules.models.ports import EmbeddingPort
from enterprise_ai_platform.modules.models.ports import ModelFailure
from enterprise_ai_platform.modules.models.service import validate_embeddings


class KnowledgeService:
    def __init__(self,repository:KnowledgeRepository,origin:str,*,clock=time.time):self.repository,self.origin,self.clock=repository,origin,clock
    def token(self,session):
        if not opaque(session):raise KnowledgeFailure(401)
        return digest('session',session)
    def csrf(self,headers):
        if headers.get('origin')!=self.origin or headers.get('content-type')!='application/json' or not opaque(headers.get('x-csrf-token')):raise KnowledgeFailure(403)
        return digest('csrf',headers['x-csrf-token'])
    async def call(self,session,operation,values):
        row=await self.repository.call(self.token(session),operation,values)
        deadline=row.pop('authority_expires_at',None)
        if type(deadline) not in (int,float) or not math.isfinite(deadline) or self.clock()>=deadline:raise KnowledgeFailure(401)
        return row
    async def create(self,session,headers,values):
        self.validate(values,False)
        return await self.call(session,'create',{**values,'id':str(uuid4()),'version_id':str(uuid4()),'csrf_hash':self.csrf(headers)})
    async def version(self,session,headers,document_id,values):
        identifier(document_id);self.validate(values,True)
        return await self.call(session,'version',{**values,'id':document_id,'version_id':str(uuid4()),'csrf_hash':self.csrf(headers)})
    async def status(self,session,document_id,version_id):
        return await self.call(session,'status',dict(id=identifier(document_id),version_id=identifier(version_id)))
    def page(self,after,limit):
        if after is not None:identifier(after)
        if type(limit) is not int or not 1<=limit<=50:raise KnowledgeFailure(400)
        return dict(after=after,limit=limit)
    async def catalog(self,session,*,after=None,limit=20,query=None,collection=None):
        payload=self.page(after,limit)
        if query is not None:
            try:
                if not isinstance(query,str) or not 1<=len(query.strip())<=200 or len(query.encode('utf-8'))>800 or any(ord(c)<32 or ord(c)==127 for c in query):raise ValueError()
            except (ValueError,UnicodeError):raise KnowledgeFailure(400) from None
        if collection is not None and collection!='tenant':identifier(collection)
        return await self.call(session,'catalog',{**payload,'query':query,'collection_id':None if collection=='tenant' else collection,'tenant_only':collection=='tenant'})
    async def detail(self,session,document_id):
        return await self.call(session,'detail',dict(id=identifier(document_id)))
    async def versions(self,session,document_id,*,after=None,limit=20):
        return await self.call(session,'versions',dict(id=identifier(document_id),**self.page(after,limit)))
    async def preview(self,session,document_id,version_id):
        payload=dict(id=identifier(document_id),version_id=identifier(version_id))
        first=await self.call(session,'preview',payload)
        return await self.call(session,'preview_recheck',{**payload,**{key:first[key] for key in ('access_revision','index_generation','content_digest')}})
    async def audience(self,session,document_id,*,after=None,limit=20):
        return await self.call(session,'audience',dict(id=identifier(document_id),**self.page(after,limit)))
    async def selection(self,session,selection_id,*,after=None,limit=20):
        return await self.call(session,'selection',dict(id=identifier(selection_id),**self.page(after,limit)))
    async def replace_audience(self,session,headers,document_id,values):
        identifier(document_id)
        if not isinstance(values,dict) or set(values)!={'audience_mode','selection_id','expected_version','key'}:raise KnowledgeFailure(400)
        identifier(values['key'])
        if type(values['expected_version']) is not int or not 1<=values['expected_version']<10**18:raise KnowledgeFailure(400)
        if values['audience_mode'] not in ('specific_people','all_current_employees'):raise KnowledgeFailure(400)
        if values['audience_mode']=='specific_people':identifier(values['selection_id'])
        elif values['selection_id'] is not None:raise KnowledgeFailure(400)
        return await self.call(session,'audience_replace',{**values,'id':document_id,'csrf_hash':self.csrf(headers)})
    async def create_selection(self,session,headers,values):
        expected={'key','collection_id','base_document_id','expected_version','all_matching','filters','include_ids','exclude_ids'}
        if not isinstance(values,dict) or not expected<=set(values) or set(values)-expected-{'retain_existing'}:raise KnowledgeFailure(400)
        identifier(values['key'])
        if values['collection_id'] is not None:identifier(values['collection_id'])
        if values['base_document_id'] is not None:
            identifier(values['base_document_id'])
            if type(values['expected_version']) is not int or not 1<=values['expected_version']<10**18:raise KnowledgeFailure(400)
        elif values['expected_version'] is not None:raise KnowledgeFailure(400)
        if type(values['all_matching']) is not bool or type(values.get('retain_existing',True)) is not bool:raise KnowledgeFailure(400)
        filters=values['filters']
        if not isinstance(filters,dict) or set(filters)-{'q','department','job_title','group'}:raise KnowledgeFailure(400)
        normalized={}
        for name in ('q','department','job_title','group'):
            value=filters.get(name)
            try:
                if value is not None:
                    if not isinstance(value,str):raise ValueError()
                    value=value.strip();maximum=200 if name=='q' else 100
                    if not 1<=len(value)<=maximum or len(value.encode('utf-8'))>maximum*4 or any(ord(c)<32 or ord(c)==127 for c in value):raise ValueError()
            except (ValueError,UnicodeError):raise KnowledgeFailure(400) from None
            normalized['query' if name=='q' else name]=value
        payload={**values,'filters':normalized,'retain_existing':values.get('retain_existing',True)}
        for name in ('include_ids','exclude_ids'):
            if not isinstance(values[name],list) or len(values[name])>1000:raise KnowledgeFailure(400)
            payload[name]=sorted({identifier(value) for value in values[name]})
        return await self.call(session,'selection_create',{**payload,'id':str(uuid4()),'csrf_hash':self.csrf(headers)})
    async def mutate(self,session,headers,operation,target_id,values):
        if operation not in ('publish','withdraw','acl'):raise KnowledgeFailure(400)
        identifier(target_id)
        expected={'expected_version','key','readers'} if operation=='acl' else {'expected_version','key'}
        if not isinstance(values,dict) or set(values)!=expected:raise KnowledgeFailure(400)
        identifier(values['key'])
        if type(values['expected_version']) is not int or not 1<=values['expected_version']<10**18:raise KnowledgeFailure(400)
        if operation=='acl':
            if not isinstance(values['readers'],list) or len(values['readers'])>50:raise KnowledgeFailure(400)
            for reader in values['readers']:identifier(reader)
            values={**values,'readers':sorted(set(values['readers']))}
        return await self.call(session,operation,{**values,'id':target_id,'csrf_hash':self.csrf(headers)})

    async def search(self,session,query,language,limit=5):
        try:query_bytes=len(query.encode('utf-8')) if isinstance(query,str) else 0
        except UnicodeError:raise KnowledgeFailure(400) from None
        if (not isinstance(query,str) or not 1<=len(query.strip())<=200 or query_bytes>800
            or any(ord(c)<32 or ord(c)==127 for c in query) or language not in ('id','en')
            or type(limit) is not int or not 1<=limit<=10):raise KnowledgeFailure(400)
        first=await self.call(session,'search',dict(query=query,language=language,limit=limit))
        if not isinstance(first.get('items'),list) or len(first['items'])>limit:raise KnowledgeFailure(503)
        bindings=[{key:hit[key] for key in ('version_id','chunk_no','index_generation','access_revision','text_digest')} for hit in first['items']]
        # Discard the first buffer. Final payload is fetched again under current
        # source/ACL checks; a revoked item is removed without leaking its count.
        return await self.call(session,'search_recheck',dict(items=bindings))

    async def source(self,session,version_id):
        first=await self.call(session,'source',dict(version_id=identifier(version_id)))
        return await self.call(session,'source_recheck',{key:first[key] for key in ('version_id','access_revision','index_generation','content_digest')})

    def validate(self,values,version):
        expected={'file_id','key','expected_version'} if version else {'file_id','key','title','readers'}
        optional={'valid_from','valid_until'} if version else {'valid_from','valid_until','audience_mode','selection_id'}
        if not isinstance(values,dict) or not expected<=set(values) or set(values)-expected-optional:raise KnowledgeFailure(400)
        try:
            parsed=[]
            for name in ('valid_from','valid_until'):
                value=values.get(name)
                if value is not None and (not isinstance(value,str) or len(value)!=10 or date.fromisoformat(value).isoformat()!=value):raise ValueError()
                parsed.append(None if value is None else date.fromisoformat(value))
            if all(value is not None for value in parsed) and parsed[1]<=parsed[0]:raise ValueError()
        except (ValueError,TypeError):raise KnowledgeFailure(400) from None
        identifier(values['file_id']);identifier(values['key'])
        if version:
            if type(values['expected_version']) is not int or not 1<=values['expected_version']<10**18:raise KnowledgeFailure(400)
        else:
            if not isinstance(values['title'],str) or not 1<=len(values['title'].strip())<=200 or any(ord(c)<32 for c in values['title']):raise KnowledgeFailure(400)
            readers=values['readers']
            if not isinstance(readers,list) or len(readers)>50:raise KnowledgeFailure(400)
            for reader in readers:identifier(reader)
            mode=values.get('audience_mode');selection=values.get('selection_id')
            if mode not in (None,'specific_people','all_current_employees'):raise KnowledgeFailure(400)
            if selection is not None:
                identifier(selection)
                if mode!='specific_people':raise KnowledgeFailure(400)
            if (mode=='all_current_employees' or selection is not None) and readers:raise KnowledgeFailure(400)


class KnowledgeIngestion:
    def __init__(self,repository,storage,extractor,jobs,embedding:EmbeddingPort,credential_hash):
        self.repository,self.storage,self.extractor,self.jobs,self.embedding=repository,storage,extractor,jobs,embedding
        self.payload=None;self.credential_hash=credential_hash
    async def call(self,operation,extra=None):
        row=await self.repository.call(None,operation,{**self.payload,**(extra or {})})
        if operation in ('begin','reference','reserve_embedding','stage'):
            deadline=row.get('authority_expires_at')
            if type(deadline) not in (int,float) or not math.isfinite(deadline) or time.time()>=deadline:raise KnowledgeFailure(403)
        return row
    def observer(self,loop):
        def reserve(kind,tokens):
            if kind!='embedding' or type(tokens) is not int or not 1<=tokens<=2048:raise KnowledgeFailure(503)
            future=asyncio.run_coroutine_threadsafe(self.call('reserve_embedding'),loop)
            try:
                row=future.result(timeout=10)
                deadline=row.get('authority_expires_at')
                if type(deadline) not in (int,float) or not math.isfinite(deadline) or time.time()>=deadline:raise KnowledgeFailure(403)
                return deadline
            except KnowledgeFailure:raise
            except Exception:future.cancel();raise KnowledgeFailure(503) from None
        return reserve
    async def process(self,claim):
        if self.payload is not None or claim.get('job_type')!='knowledge.ingest':raise KnowledgeFailure(400)
        lease=Lease(UUID(claim['id']),UUID(claim['tenant_id']),claim['generation'])
        self.payload=dict(credential_hash=self.credential_hash,job_id=str(lease.id),generation=lease.generation)
        async def heartbeat():
            while True:
                await asyncio.sleep(4);await self.jobs.heartbeat(lease)
        async def ingest():
            source=await self.call('begin',dict(parser_ref=self.extractor.ref))
            await self.call('reference')
            data=await self.storage.read(source)
            if len(data)!=source['size_bytes'] or hashlib.sha256(data).hexdigest()!=source['digest']:raise KnowledgeFailure(422)
            await self.call('reference')
            pages=await self.extractor.extract(data,source['mime']);parts=chunks(pages)
            for offset in range(0,len(parts),4):
                await self.call('reference')
                batch=parts[offset:offset+4]
                # One synchronous provider request at a time. Its observer rechecks
                # object authority and writes the paid reservation immediately before egress.
                pending=asyncio.create_task(asyncio.to_thread(self.embedding.embed,[part.text for part in batch],input_type='search_document'))
                try:result=await asyncio.shield(pending)
                except asyncio.CancelledError:
                    try:await asyncio.shield(pending)
                    except Exception:pass
                    raise
                checked=validate_embeddings([list(row) for row in result.vectors],len(batch))
                staged=[{**asdict(part),'vector':list(vector),'model':MODEL} for part,vector in zip(batch,checked)]
                await self.call('stage',dict(chunks=staged))
            renewal.cancel();await asyncio.gather(renewal,return_exceptions=True)
            content=hashlib.sha256(json.dumps([dict(page=p.number,text=p.text) for p in pages],ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
            return await self.call('ready',dict(chunk_count=len(parts),content_digest=content))
        try:
            async with asyncio.timeout(100):
                async with asyncio.TaskGroup() as group:
                    renewal=group.create_task(heartbeat())
                    try:return await ingest()
                    finally:renewal.cancel()
        except BaseException as error:
            if isinstance(error,(KeyboardInterrupt,SystemExit,asyncio.CancelledError)):raise
            def category(value):
                if isinstance(value,BaseExceptionGroup):return next((category(e) for e in value.exceptions if category(e)!='transient'),'transient')
                if isinstance(value,KnowledgeFailure):return 'invalid' if value.status in (400,413,422) else 'forbidden' if value.status in (401,403,404,409) else 'transient'
                if isinstance(value,ModelFailure):return 'invalid' if value.code in ('invalid_input','invalid_output') else 'rate_limited' if value.code=='rate_limited' else 'transient'
                return 'transient'
            code=category(error)
            try:await self.call('failed',dict(error_code=code))
            except Exception:
                try:await self.jobs.fail(lease,'forbidden')
                except Exception:pass
            raise KnowledgeFailure(503) from None
        finally:self.payload=None
