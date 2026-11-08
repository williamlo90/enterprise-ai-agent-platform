"""Bounded agent tools over shared manual drafts and durable UI actions."""
import asyncio
import hashlib
import json
from enterprise_ai_platform.modules.assisted.domain import PREPARE,proposal,content
from enterprise_ai_platform.modules.conversations.ports import ConversationFailure

class AssistedTurn:
    def __init__(self,repository,saver,source,models,jobs,lease,budget):
        self.repository,self.saver,self.source,self.models,self.jobs,self.lease,self.budget=repository,saver,source,models,jobs,lease,budget

    async def read(self):
        data=await self.repository.call('input',{})
        selected=data['selection'];facts=await self.source.read(data['context'],selected['trip_id'],selected['claim_id'])
        return data,facts

    async def prepare(self):
        data,facts=await self.read();saved=data['proposal']
        if saved is None:
            # One durable reservation per run. A crash after sending but before
            # saving the proposal holds; it never silently sends a second call.
            async def reserve(kind,maximum):
                if kind!='chat' or maximum!=4096:raise ConversationFailure(503)
                digest=hashlib.sha256(json.dumps(dict(question=data['question'],trip=facts.wire()['material']['trip'],selection=data['selection']),sort_keys=True).encode()).hexdigest()
                return await self.budget.call('reserve',dict(kind='chat',request_digest=digest,items=[]))
            loop=asyncio.get_running_loop();receipt=[]
            def before_call(kind,maximum):
                value=asyncio.run_coroutine_threadsafe(reserve(kind,maximum),loop).result(timeout=10);receipt.append(value['settlement_receipt'])
                return value['authority_expires_at']
            with self.models(before_call) as model:
                task=asyncio.create_task(asyncio.to_thread(model.propose,data['question'],facts.wire()['material']['trip'],data['selection']))
                try:saved=await asyncio.shield(task)
                finally:
                    # Drain the bounded SDK request before releasing resources.
                    if not task.done():
                        try:await task
                        except Exception:pass
                    if receipt:await self.budget.settle('chat',receipt[0],model.last_usage)
                saved=proposal(saved)
                await self.repository.call('proposal',dict(proposal=saved))
        if saved['name']!=PREPARE:return 'clarification'
        # Both replay keys are server-derived by SQL from the immutable run.
        created=await self.repository.call('create',dict(source=None))
        if created['draft'] is None:created=await self.repository.call('create',dict(source=facts.wire()))
        draft=created['draft']
        prepared=content(self.repository.binding['run_id'],saved,data['selection'],facts.wire()['material']['trip'])
        await self.repository.call('replace',dict(content=prepared))
        return 'prepared'

    async def gate(self):
        # This ACK does not issue consent, enqueue dispatch or accept resume data.
        result=await self.repository.call('status',{})
        if result['action'] is not None:return 'acknowledged'
        data,facts=await self.read()
        await self.repository.call('preview',dict(source=facts.wire()))
        return 'human_confirmation_required'

    async def run(self,graph_factory,*,resume_input=None):
        async def heartbeat():
            while True:
                await asyncio.sleep(4)
                async with self.repository.lease_slot():await self.jobs.heartbeat(self.lease)
        pulse=asyncio.create_task(heartbeat())
        try:
            graph=graph_factory(self)
            # Only phase/codes persist. IDs/facts/proposals are read from the
            # private run-owned request, never trusted from Command.resume.
            async with asyncio.timeout(50):
                result=await graph.ainvoke(resume_input if resume_input is not None else {'phase':'new'},self.saver.config())
            await self.saver.finish('waiting' if result.get('__interrupt__') else 'completed')
            return result
        except Exception:
            try:await self.saver.finish('failed')
            except Exception:pass
            raise
        finally:
            pulse.cancel()
            try:await pulse
            except asyncio.CancelledError:pass
