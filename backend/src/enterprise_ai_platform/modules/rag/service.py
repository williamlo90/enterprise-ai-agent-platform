"""Bounded single-turn retrieval/selection. The model cannot produce free claims."""
import asyncio
import math
import time
from threading import Event
from dataclasses import asdict
from enterprise_ai_platform.modules.rag.domain import RagFailure,INTRO,Context,context,bindings,parse_proposal,render,sha,PROMPT_VERSION,CANDIDATE_PROMPT_VERSION
from enterprise_ai_platform.modules.models.ports import Message,Usage,ModelFailure,ResponseContract
from enterprise_ai_platform.modules.models.service import validate_embeddings
from enterprise_ai_platform.modules.jobs.ports import Lease

from enterprise_ai_platform.modules.conversations.ports import SourceRef

def sources(hits):return tuple(dict.fromkeys(SourceRef('platform','document_version',v['version_id'],str(v['version_no']),'read',str(v['access_revision']),v['page'],v['chunk_no']) for v in hits))

async def drain_tasks(tasks):
    """Keep cleanup owned and awaitable even through repeated caller cancellation."""
    pending=asyncio.gather(*tasks,return_exceptions=True);cancelled=None
    while not pending.done():
        try:await asyncio.shield(pending)
        except asyncio.CancelledError as error:cancelled=error
    pending.result()
    if cancelled is not None:raise cancelled

class RagTurn:
    def __init__(self,repository,model_factory,saver,jobs,lease:Lease,*,mode='vector',clock=time.time,prompt_version=PROMPT_VERSION):
        if mode not in ('vector','hybrid','rerank'):raise ValueError('Unknown retrieval mode.')
        if prompt_version not in (PROMPT_VERSION,CANDIDATE_PROMPT_VERSION):raise ValueError('Unknown RAG prompt version.')
        self.prompt_version=prompt_version
        self.repository,self.model_factory,self.saver,self.jobs,self.lease=repository,model_factory,saver,jobs,lease
        self.mode=mode;self.ctx=None;self.question='';self.language='en';self.active_kind=None;self.buffer=None;self.started=False
        self.reserved=set()
        self.clock=clock
        self.receipts={};self.stopping=Event();self.deadline=None

    async def event(self,phase,outcome,started=None):
        elapsed=0 if started is None else max(0,min(3600000,int((time.monotonic()-started)*1000)))
        await self.repository.runtime('event',dict(phase=phase,outcome=outcome,duration_ms=elapsed))

    def observer(self,loop):
        def before_call(kind,tokens):
            if kind!=self.active_kind or self.stopping.is_set():raise RagFailure(503)
            items=bindings(self.ctx.hits) if kind=='chat' and self.ctx else []
            text=self.ctx.prompt if kind=='chat' else self.question
            future=asyncio.run_coroutine_threadsafe(self.repository.call('reserve',dict(kind=kind,request_digest=sha(text),items=items)),loop)
            try:
                row=future.result(timeout=10);deadline=row['authority_expires_at'];self.reserved.add(kind)
                self.receipts[kind]=row['settlement_receipt']
                if self.stopping.is_set() or type(deadline) not in (int,float) or not math.isfinite(deadline) or self.clock()>=deadline:raise RagFailure(401)
                return deadline
            except RagFailure:raise
            except Exception:future.cancel();raise RagFailure(503) from None
        return before_call

    async def provider(self,kind,adapter,function):
        if self.stopping.is_set():raise RagFailure(409)
        self.active_kind=kind
        # Recheck immediately before starting the provider adapter. The adapter's
        # observer durably reserves/rechecks again immediately before its SDK call.
        await self.repository.call('recheck',dict(items=bindings(self.ctx.hits) if self.ctx else []))
        async def complete():
            try:
                value=await asyncio.to_thread(function)
                if kind not in self.reserved:raise RagFailure(503)
                return value
            finally:
                usage=adapter.last_usage
                if kind in self.receipts:await self.repository.settle(kind,self.receipts[kind],usage if isinstance(usage,Usage) else None)
                self.active_kind=None
        cleanup=asyncio.create_task(complete())
        try:return await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await drain_tasks((cleanup,))
            raise

    async def retrieve(self,embedding):
        await self.repository.runtime('step',dict(phase='retrieve'))
        row=await self.repository.call('input',{})
        self.language=row['language'];self.question=row['question']
        if row.get('prior_question'):self.question=row['prior_question']+' '+self.question
        if row.get('clarification_rounds',0)>=3:
            self.ctx=Context('',(),sha('empty'));self.buffer=render(dict(kind='insufficient_evidence',citations=[]),self.ctx,self.language);return
        if len(self.question)>200 or len(self.question.encode('utf-8'))>400:
            self.ctx=Context('',(),sha('empty'));self.buffer=render(dict(kind='clarification',citations=[]),self.ctx,self.language);return
        try:
            vector=await self.provider('embedding',embedding,lambda:embedding.embed([self.question],input_type='search_query'))
            checked=validate_embeddings([list(v) for v in vector.vectors],1)
        except ModelFailure:
            self.ctx=Context('',(),sha('empty'));self.buffer=dict(kind='service_unavailable',text=INTRO[self.language]['service_unavailable'],citations=[]);return
        found=await self.repository.call('search',dict(query=self.question,vector=list(checked[0]),mode=self.mode))
        self.ctx=context(self.question,self.language,found['items'],prompt_version=self.prompt_version)
        await self.repository.call('recheck',dict(items=bindings(self.ctx.hits)))
        self.saver.binding['sources']=[asdict(v) for v in sources(self.ctx.hits)]
        if not self.ctx.hits:self.buffer=render(dict(kind='insufficient_evidence',citations=[]),self.ctx,self.language)

    async def generate(self,chat):
        await self.repository.runtime('step',dict(phase='generate'))
        if self.buffer:return
        try:
            contract=ResponseContract.EVIDENCE_SELECTION if self.prompt_version==CANDIDATE_PROMPT_VERSION else None
            result=await self.provider('chat',chat,lambda:chat.chat([Message('user',text=self.ctx.prompt,response_contract=contract)],tools=False))
            if result.tool is not None:raise RagFailure(503)
            self.buffer=render(parse_proposal(result.text,self.ctx),self.ctx,self.language)
        except (ModelFailure,RagFailure) as error:
            if isinstance(error,RagFailure) and error.status!=503:raise
            self.buffer=dict(kind='service_unavailable',text=INTRO[self.language]['service_unavailable'],citations=[])

    async def run(self,graph_factory):
        if self.started:raise RagFailure(409)
        self.started=True
        try:runtime=await self.repository.runtime('begin',{})
        except BaseException:
            self.stopping.set()
            try:await self.repository.call('hold',{})
            except Exception:pass
            raise
        self.deadline=runtime['deadline'];remaining=self.deadline-self.clock()
        if not math.isfinite(remaining) or remaining<=0:raise RagFailure(429)
        loop=asyncio.get_running_loop();began=time.monotonic()
        async def heartbeat():
            while True:
                await asyncio.sleep(4)
                async with self.repository.lease_slot():
                    result=await self.jobs.heartbeat(self.lease)
                    if result.get('status')!='ok':raise RagFailure(409)
        async def execute():
            try:
                with self.model_factory(self.observer(loop)) as models:
                    await graph_factory(self,models).ainvoke({'phase':'queued'},self.saver.config())
            except ModelFailure:
                self.buffer=dict(kind='service_unavailable',text=INTRO[self.language]['service_unavailable'],citations=[])
                if self.ctx is None:self.ctx=Context('',(),sha('empty'))
            if self.buffer is None or self.ctx is None:raise RagFailure(503)
            await self.event('publish','started',began)
            return await self.repository.call('publish',dict(items=bindings(self.ctx.hits),**self.buffer))
        beat=asyncio.create_task(heartbeat());work=asyncio.create_task(execute())
        try:
            async with asyncio.timeout(remaining):
                done,_=await asyncio.wait((beat,work),return_when=asyncio.FIRST_COMPLETED)
                if work in done:return await work
                await beat;raise RagFailure(503)
        except BaseException as error:
            self.stopping.set()
            for task in (beat,work):
                if not task.done():task.cancel()
            try:await self.event('hold','deadline' if isinstance(error,TimeoutError) else 'cancelled' if isinstance(error,asyncio.CancelledError) else 'failed',began)
            except Exception:pass
            try:await self.repository.call('hold',{})
            except Exception:pass
            raise
        finally:
            for task in (beat,work):
                if not task.done():task.cancel()
            await drain_tasks((beat,work))
