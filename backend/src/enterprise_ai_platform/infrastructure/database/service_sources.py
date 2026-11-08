"""Bounded staged verification; remote callbacks never run in the owner UoW."""
import asyncio
import copy
import time
from enterprise_ai_platform.modules.conversations.ports import ConversationFailure

KEYS=('tenant_id','actor_id','membership_id','session_id','access_revision')
class ServiceStage(Exception):
    def __init__(self,context,refs,signature):
        super().__init__('Live source verification required.')
        self.context=copy.deepcopy(context);self.refs=tuple(refs);self.signature=copy.deepcopy(signature)

class ServiceGate:
    def __init__(self,stage,first,second,*,closed=False):
        self.stage=stage;self.expires_at=min(first.expires_at,second.expires_at);self.envelopes=copy.deepcopy(second.envelopes)
        self.allowed={r:bool(not closed and first.allowed.get(r) and second.allowed.get(r) and first.fingerprints.get(r)==second.fingerprints.get(r)) for r in stage.refs if r.system=='service'}
        self.fingerprints=copy.deepcopy(second.fingerprints)
    def merge(self,access,refs,local,signature):
        if tuple(access.service_context.get(k) for k in KEYS)!=tuple(self.stage.context.get(k) for k in KEYS) or frozenset(refs)!=frozenset(self.stage.refs) or signature!=self.stage.signature:raise ConversationFailure(409)
        if time.time()>=self.expires_at:raise ConversationFailure(403)
        return tuple((bool(self.allowed.get(r)) and access.service_eligible[n]) if r.system=='service' else v for n,(r,v) in enumerate(zip(refs,local)))

async def _staged(local,reader,*,redact=False):
    try:return await local(None)
    except ServiceStage as value:first=value
    async with asyncio.timeout(20):
        a=await reader.verify(first.context,first.refs)
        try:await local(None)
        except ServiceStage as value:second=value
        else:raise ConversationFailure(409)
        if tuple(first.context.get(k) for k in KEYS)!=tuple(second.context.get(k) for k in KEYS) or frozenset(first.refs)!=frozenset(second.refs) or first.signature!=second.signature:raise ConversationFailure(409)
        b=await reader.verify(second.context,second.refs);gate=ServiceGate(second,a,b)
        result=await local(gate)
        if any(gate.allowed.values()):
            c=await reader.verify(second.context,second.refs)
            stable=all(not v or c.allowed.get(r) and c.fingerprints.get(r)==gate.fingerprints.get(r) for r,v in gate.allowed.items()) and time.time()<min(gate.expires_at,c.expires_at)
            if not stable:
                if not redact:raise ConversationFailure(403)
                gate=ServiceGate(second,a,c,closed=True);result=await local(gate)
            else:
                # The final remote await released all local locks. Recheck the
                # original owner snapshot, document ACL and receipt/file gates
                # using the newest envelope before releasing any buffered data.
                gate=ServiceGate(second,b,c);result=await local(gate)
        deadline=result.get('authority_expires_at')
        if type(deadline) in (int,float):result['authority_expires_at']=min(deadline,gate.expires_at)
        if time.time()>=gate.expires_at:raise ConversationFailure(403)
        from enterprise_ai_platform.modules.identity.ports import browser_read_scope
        scope=browser_read_scope.get()
        if scope is not None and isinstance(scope.attestation,dict):
            scope.check_owner();scope.attestation={**scope.attestation,'authority_expires_at':min(scope.attestation['authority_expires_at'],gate.expires_at)}
        return result

async def staged(local,reader,*,redact=False):
    from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure
    try:return await _staged(local,reader,redact=redact)
    except (ConversationFailure,BrowserContextFailure):raise
    except Exception:raise ConversationFailure(503) from None
