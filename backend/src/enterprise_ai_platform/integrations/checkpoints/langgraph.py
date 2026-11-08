"""Async-only bound saver. Thread/namespace are routing, never authority.

JSON and explicitly supported Interrupt values only: no pickle/type imports.
An instance is bound server-side to one exact tenant/run/job/lease/epoch.
"""
from dataclasses import asdict
import asyncio
import json
import math
import re
from uuid import UUID
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP
from langgraph.types import Interrupt
from enterprise_ai_platform.modules.conversations.ports import ConversationFailure, SourceRef


def encode(value, depth=0):
    if depth>24:raise ConversationFailure(400)
    if isinstance(value,Interrupt):return {'$interrupt':{'value':encode(value.value,depth+1),'id':value.id}}
    if value is None or type(value) in (str,bool,int):return value
    if type(value) is float and math.isfinite(value):return value
    if isinstance(value,(list,tuple)):return [encode(v,depth+1) for v in value]
    if type(value) is dict:
        if any(not isinstance(k,str) or k.startswith('$') or k.lower() in {'password','secret','token','authorization','credential','raw_receipt'} for k in value):raise ConversationFailure(400)
        return {k:encode(v,depth+1) for k,v in value.items()}
    raise ConversationFailure(400)


def decode(value):
    if isinstance(value,list):return [decode(v) for v in value]
    if isinstance(value,dict):
        if set(value)=={'$interrupt'}:
            v=value['$interrupt'];return Interrupt(value=decode(v['value']),id=v['id'])
        return {k:decode(v) for k,v in value.items()}
    return value


class BoundCheckpointSaver(BaseCheckpointSaver):
    """Only async methods supported; unsupported mutation APIs fail closed."""
    def __init__(self, repository, *, credential_hash, tenant_id, run_id, job_id, generation, epoch=0, sources=()):
        super().__init__()
        for value in (tenant_id,run_id,job_id):
            if str(UUID(value))!=value:raise ValueError('Canonical UUID required.')
        if not re.fullmatch('[a-f0-9]{64}',credential_hash) or type(generation) is not int or generation<1 or type(epoch) is not int or epoch<0:raise ValueError('Invalid binding.')
        if len(sources)>8 or any(not isinstance(v,SourceRef) for v in sources):raise ValueError('Invalid dependencies.')
        self.repository=repository
        self._persisted={}
        self.binding=dict(credential_hash=credential_hash,tenant_id=tenant_id,run_id=run_id,job_id=job_id,generation=generation,epoch=epoch,sources=[asdict(v) for v in sources])

    def payload(self,config=None,**extra):
        if config is None:raise ConversationFailure(400)
        c=config.get('configurable',{})
        if c.get('thread_id')!=self.binding['run_id']:raise ConversationFailure(403)
        ns=c.get('checkpoint_ns','')
        if not isinstance(ns,str) or len(ns)>256 or any(ord(v)<32 for v in ns):raise ConversationFailure(400)
        checkpoint=c.get('checkpoint_id')
        if checkpoint is not None:
            try:
                if str(UUID(checkpoint))!=checkpoint:raise ValueError
            except (ValueError,TypeError,AttributeError):raise ConversationFailure(400) from None
        return {**self.binding,'namespace':ns,'checkpoint_id':checkpoint,**extra}

    async def start(self, *, model_ref, prompt_version, config_version):
        result=await self.repository.call('start',self.payload(self.config(),model_ref=model_ref,prompt_version=prompt_version,config_version=config_version))
        self.binding['epoch']=result['epoch'];return result

    def config(self,namespace='',checkpoint_id=None):
        result={'thread_id':self.binding['run_id'],'checkpoint_ns':namespace}
        if checkpoint_id is not None:result['checkpoint_id']=checkpoint_id
        return {'configurable':result}

    async def finish(self, state):
        if state not in ('waiting','completed','failed'):raise ConversationFailure(400)
        return await self.repository.call('finish',self.payload(self.config(),state=state))

    async def aget_tuple(self,config):
        result=await self.repository.call('get',self.payload(config))
        return self.unpack(result['items'][0]) if result['items'] else None

    def unpack(self,item):
        self.persisted(item['namespace'],item['id']).set()
        return CheckpointTuple(self.config(item['namespace'],item['id']),decode(item['payload']['checkpoint']),decode(item['payload']['metadata']),self.config(item['namespace'],item['parent_id']) if item['parent_id'] else None,[(v['task_id'],v['channel'],decode(v['value'])) for v in item['writes']])

    def persisted(self,namespace,checkpoint_id):
        key=(namespace,checkpoint_id)
        if key not in self._persisted:
            if len(self._persisted)>=128:raise ConversationFailure(429)
            self._persisted[key]=asyncio.Event()
        return self._persisted[key]

    async def alist(self,config,*,filter=None,before=None,limit=None):
        if filter is not None and (type(filter) is not dict or len(filter)>8):raise ConversationFailure(400)
        if limit is not None and (type(limit) is not int or not 1<=limit<=20):raise ConversationFailure(400)
        before_id=self.payload(before)['checkpoint_id'] if before is not None else None
        result=await self.repository.call('list',self.payload(config,before=before_id,limit=limit or 20,filter=encode(filter or {})))
        for item in result['items']:yield self.unpack(item)

    async def aput(self,config,checkpoint,metadata,new_versions):
        payload=encode(dict(checkpoint=checkpoint,metadata=metadata))
        if len(json.dumps(payload,ensure_ascii=False).encode())>65536:raise ConversationFailure(400)
        new_id=checkpoint['id'];self.payload(self.config(checkpoint_id=new_id))
        await self.repository.call('put',self.payload(config,new_id=new_id,payload=payload))
        self.persisted(config['configurable'].get('checkpoint_ns',''),new_id).set()
        return self.config(config['configurable'].get('checkpoint_ns',''),new_id)

    async def aput_writes(self,config,writes,task_id,task_path=''):
        if not isinstance(task_id,str) or len(task_id)>128 or not isinstance(task_path,str) or len(task_path)>256 or len(writes)>32:raise ConversationFailure(400)
        values=[dict(index=WRITES_IDX_MAP.get(channel,i),channel=channel,value=encode(value)) for i,(channel,value) in enumerate(writes)]
        binding=self.payload(config)
        if binding['checkpoint_id'] is None:raise ConversationFailure(400)
        ready=self.persisted(binding['namespace'],binding['checkpoint_id'])
        if not ready.is_set():
            # Async durability can schedule pending writes before their parent put.
            # Wait outside any DB/source guard; existing rows are loaded normally.
            await self.aget_tuple(config)
            try:
                async with asyncio.timeout(5):await ready.wait()
            except TimeoutError:raise ConversationFailure(503) from None
        await self.repository.call('writes',self.payload(config,task_id=task_id,task_path=task_path,writes=values))
