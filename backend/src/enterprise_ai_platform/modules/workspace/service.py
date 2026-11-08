"""Browser workflow facade: service state and delivery intent stay separate."""
import time
import math
from enterprise_ai_platform.modules.workspace.domain import query,identifier
from enterprise_ai_platform.modules.actions.ports import ActionFailure
from enterprise_ai_platform.modules.identity.service import opaque,digest

class WorkspaceService:
    def __init__(self,repository,reader,draft_source,*,clock=time.time):self.repository,self.reader,self.draft_source,self.clock=repository,reader,draft_source,clock
    def token(self,session):
        if not opaque(session):raise ActionFailure(401)
        return digest('session',session)
    def live(self,data):
        expiry=data.get('authority_expires_at')
        if type(expiry) not in (int,float) or not math.isfinite(expiry) or self.clock()>=expiry:raise ActionFailure(401)
    async def recheck(self,token,view,first):
        current=await self.repository.call(token,'context',dict(view=view),final=True);self.live(first);self.live(current)
        if any(current[k]!=first[k] for k in ('tenant_id','actor_id','membership_id','session_id','access_revision')):raise ActionFailure(409)
        return current
    async def read(self,session,view,*,claim_id=None,after=None,limit=20):
        try:values=query(view,claim_id,after,limit)
        except (ValueError,TypeError):raise ActionFailure(400) from None
        token=self.token(session);first=await self.repository.call(token,'context',dict(view=view));self.live(first)
        # A second current source read discards the first snapshot. It prevents
        # an inbox/detail read raced by stage movement from returning old data.
        await self.reader.read(first,values)
        await self.recheck(token,view,first)
        result,deadline=await self.reader.read(first,values)
        current=await self.recheck(token,view,first)
        if self.clock()>=deadline:raise ActionFailure(503)
        return dict(result,_workspace_authority_deadline=int(min(deadline,current['authority_expires_at'],first['authority_expires_at'])))
    async def trip(self,session,trip_id):
        try:identifier(trip_id)
        except (ValueError,TypeError):raise ActionFailure(400) from None
        token=self.token(session);first=await self.repository.call(token,'context',dict(view='mine'));self.live(first)
        facts=await self.draft_source.read(first,trip_id,None)
        current=await self.recheck(token,'mine',first);material=facts.wire()['material']
        if self.clock()>=material['expires_at']:raise ActionFailure(503)
        return dict(trip=material['trip'],_workspace_authority_deadline=int(min(material['expires_at'],current['authority_expires_at'],first['authority_expires_at'])))
    async def owned(self,session,kind,*,after=None,limit=20):
        try:query('mine',None,after,limit)
        except (ValueError,TypeError):raise ActionFailure(400) from None
        if kind not in {'drafts','actions'}:raise ActionFailure(400)
        row=await self.repository.call(self.token(session),kind,dict(view='mine',after=after,limit=limit),final=True);self.live(row)
        deadline=row.pop('authority_expires_at');return dict(row,_workspace_authority_deadline=int(deadline))

    async def rules(self,session,draft_id,material_hash):
        try:
            identifier(draft_id)
            if type(material_hash) is not str or len(material_hash)!=64 or any(c not in '0123456789abcdef' for c in material_hash):raise ValueError
        except (ValueError,TypeError):raise ActionFailure(400) from None
        row=await self.repository.call(self.token(session),'rules',dict(view='mine',id=draft_id,material_hash=material_hash),final=True);self.live(row)
        deadline=row.pop('authority_expires_at');return dict(row,_workspace_authority_deadline=int(deadline))
