import asyncio
import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import httpx
import pytest
from enterprise_ai_platform.modules.knowledge.domain import Page,chunks,KnowledgeFailure
from enterprise_ai_platform.modules.knowledge.service import KnowledgeService,KnowledgeIngestion
from enterprise_ai_platform.modules.models.domain import EmbeddingResult,Usage
from enterprise_ai_platform.composition import ApplicationServices
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.main import create_app


def test_chunks_unicode_offsets_page_and_digest_are_stable():
    pages=[Page(1,'Kebijakan perjalanan '+('界'*140)),Page(2,'English policy after returning.')]
    result=chunks(pages)
    assert result==chunks(pages) and [r.number for r in result]==list(range(len(result)))
    for r in result:
        original=pages[r.page-1].text
        assert r.text==original[r.start:r.end] and len(r.text.encode('utf-8'))<=384
        assert r.digest==hashlib.sha256(r.text.encode('utf-8')).hexdigest()
    assert ''.join(r.text for r in result if r.page==1)==pages[0].text
    assert 'English' not in repr(result[-1])

@pytest.mark.parametrize('pages',[
 [],[Page(True,'x')],[Page(0,'x')],[Page(2,'x'),Page(1,'y')],
 [Page(1,' ')],[Page(1,'bad\x00text')],[Page(1,'x'*12289)],
 [Page(n+1,'x'*384) for n in range(33)],
])
def test_chunk_rejects_empty_malformed_and_excess_work(pages):
    with pytest.raises(KnowledgeFailure):chunks(pages)


def values():return dict(file_id=str(uuid4()),key=str(uuid4()),title='Fictional policy',readers=[])
def headers():return {'origin':'https://test.example','content-type':'application/json','x-csrf-token':'c'*43}

@pytest.mark.parametrize('change',[
 {'file_id':'foreign/notuuid'},{'tenant_id':str(uuid4())},{'readers':['bad']},{'title':' '},{'title':'x'*201},
 {'readers':['x']*51},{'title':'bad\nheading'},
])
def test_admission_validation_rejects_unsafe_payloads_without_db(change):
    repo=SimpleNamespace(call=AsyncMock());service=KnowledgeService(repo,'https://test.example')
    with pytest.raises(KnowledgeFailure):asyncio.run(service.create('s'*43,headers(),{**values(),**change}))
    repo.call.assert_not_called()


def test_authority_expired_during_final_db_await_does_not_release_status():
    repo=SimpleNamespace(call=AsyncMock(return_value=dict(document_id=str(uuid4()),authority_expires_at=10)))
    service=KnowledgeService(repo,'https://test.example',clock=lambda:10)
    with pytest.raises(KnowledgeFailure) as error:asyncio.run(service.status('s'*43,str(uuid4()),str(uuid4())))
    assert error.value.status==401


def test_http_routes_strict_input_no_query_no_publish_and_safe_result():
    service=SimpleNamespace(create=AsyncMock(return_value=dict(index_state='pending',publication_state='draft')),
      version=AsyncMock(),status=AsyncMock())
    async def run():
        app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=SimpleNamespace(),knowledge=service))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test.example') as client:
            h={**headers(),'cookie':'__Host-eai-session='+'s'*43}
            response=await client.post('/api/v1/knowledge/documents',json=values(),headers=h)
            assert response.status_code==202 and response.json()['publication_state']=='draft'
            assert response.headers['cache-control']=='no-store'
            assert (await client.post('/api/v1/knowledge/documents',json={**values(),'tenant_id':str(uuid4())},headers=h)).status_code==422
            assert (await client.post('/api/v1/knowledge/documents?tenant=x',json=values(),headers=h)).status_code==400
            assert (await client.post('/api/v1/knowledge/versions/'+str(uuid4())+'/publish',json={},headers=h)).status_code==422
    asyncio.run(run())


class Repository:
    def __init__(self,data):self.data=data;self.events=[];self.staged=[];self.deny=None
    async def call(self,token,operation,payload):
        self.events.append(operation)
        if operation==self.deny:raise KnowledgeFailure(403)
        if operation=='stage':self.staged.extend(payload['chunks'])
        if operation=='ready':return dict(index_state='ready',publication_state='draft')
        return dict(size_bytes=len(self.data),digest=hashlib.sha256(self.data).hexdigest(),mime='text/plain',authority_expires_at=time.time()+30)


def pipeline(text='Fictional travel policy.',*,bad_vector=False,fail_second=False):
    data=text.encode();repo=Repository(data)
    storage=SimpleNamespace(read=AsyncMock(return_value=data))
    extractor=SimpleNamespace(ref='fixture:utf8-v1',extract=AsyncMock(return_value=(Page(1,text),)))
    jobs=SimpleNamespace(heartbeat=AsyncMock(),fail=AsyncMock())
    worker=KnowledgeIngestion(repo,storage,extractor,jobs,None,'h'*64)
    class Embedding:
        calls=0
        def embed(self,texts,*,input_type):
            self.calls+=1;self.observer('embedding',len(texts)*512)
            assert input_type=='search_document'
            if fail_second and self.calls==2:raise RuntimeError('private provider error')
            vector=tuple([1.0]+[0.0]*(1022 if bad_vector else 1023))
            return EmbeddingResult(tuple(vector for _ in texts),Usage(len(texts)*10,0))
    embedding=Embedding();worker.embedding=embedding
    claim=dict(id=str(uuid4()),tenant_id=str(uuid4()),generation=1,job_type='knowledge.ingest')
    async def run():
        embedding.observer=worker.observer(asyncio.get_running_loop())
        return await worker.process(claim)
    return repo,worker,embedding,storage,run


def test_ingestion_writes_reserved_validated_batches_before_ready():
    repo,worker,embedding,storage,run=pipeline('x'*2000)
    result=asyncio.run(run())
    assert result==dict(index_state='ready',publication_state='draft')
    assert embedding.calls==2 and len(repo.staged)==6
    assert repo.events.count('reserve_embedding')==2 and repo.events[-1]=='ready'
    for part in repo.staged:assert len(part['vector'])==1024 and part['model']=='cohere.embed-multilingual-v3'
    assert worker.payload is None

@pytest.mark.parametrize('bad_vector,fail_second',[(True,False),(False,True)])
def test_failure_does_not_mark_partial_results_ready(bad_vector,fail_second):
    repo,_,_,_,run=pipeline('x'*2000,bad_vector=bad_vector,fail_second=fail_second)
    with pytest.raises(KnowledgeFailure):asyncio.run(run())
    assert 'ready' not in repo.events and repo.events[-1]=='failed'
    assert len(repo.staged)==(4 if fail_second else 0)


def test_authority_loss_before_embedding_reservation_prevents_stage():
    repo,_,embedding,_,run=pipeline();repo.deny='reserve_embedding'
    with pytest.raises(KnowledgeFailure):asyncio.run(run())
    assert not repo.staged and 'ready' not in repo.events


def test_changed_storage_bytes_rejected_before_extract_or_embedding():
    repo,worker,embedding,storage,run=pipeline();storage.read.return_value=b'changed'
    with pytest.raises(KnowledgeFailure):asyncio.run(run())
    assert embedding.calls==0 and not repo.staged


def test_expired_reservation_response_blocks_network_after_db_commit():
    async def run():
        repo=SimpleNamespace(call=AsyncMock(return_value=dict(authority_expires_at=0)))
        worker=KnowledgeIngestion(repo,None,None,None,None,'h'*64)
        worker.payload=dict(job_id=str(uuid4()),generation=1,credential_hash='h'*64)
        reserve=worker.observer(asyncio.get_running_loop())
        with pytest.raises(KnowledgeFailure) as error:await asyncio.to_thread(reserve,'embedding',512)
        assert error.value.status==403
        assert repo.call.await_count==1
    asyncio.run(run())



@pytest.mark.parametrize('usage',[Usage(23,0),None])
def test_provider_audit_retains_known_usage_on_validation_failure(usage):
    from enterprise_ai_platform.knowledge_check import AuditedEmbedding
    class Broken:
        last_usage=usage
        def embed(self,*args,**kwargs):raise RuntimeError('private response error')
    attempts=[dict(state='pending',usage=None,actual_token_cost_usd=None)];saved=[]
    wrapper=AuditedEmbedding(Broken(),attempts,lambda:saved.append(True))
    with pytest.raises(RuntimeError):wrapper.embed(['fictional'],input_type='search_document')
    assert saved==[True]
    assert attempts[0]['state']==('failed' if usage else 'unknown_or_failed')
    assert attempts[0]['usage']==(dict(input_tokens=23,output_tokens=0) if usage else None)
    assert attempts[0]['actual_token_cost_usd']==('0.0000023' if usage else None)
